# swarmd on AWS.
#
# Scoped to what docs/DEPLOYMENT.md actually justifies and no further. Every
# resource here exists because something in the system needs it; the things
# deliberately absent (multi-region, service mesh, ElastiCache) are listed in
# DEPLOYMENT.md section 8 with reasons rather than left as gaps.
#
# Cost, stated up front because it is the uncomfortable number: roughly
# $280/month, against effectively $0 of LLM spend. Infrastructure costs about
# 5,600x more than inference here. If the goal were minimising cost this belongs
# on Fargate or a single VM; it is on EKS because the goal is operating it.

terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }

  # Remote state with locking. Local state means two people applying at once
  # silently corrupt each other's view of reality, and the corruption is only
  # discovered when a destroy takes something it should not have.
  backend "s3" {
    key          = "swarmd/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = "swarmd"
      Environment = var.environment
      ManagedBy   = "terraform"
      # Cost allocation needs a tag on everything or the monthly bill is one
      # undifferentiated number and nobody can answer "what got expensive".
      CostCenter = "swarmd-${var.environment}"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = "swarmd-${var.environment}"

  # Two AZs, not three. Multi-AZ RDS needs two; a third adds cross-AZ data
  # transfer cost and a third NAT gateway for availability this workload does
  # not require -- runs are checkpointed and resumable, so an AZ loss costs a
  # restart rather than a result.
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

# --- network ---------------------------------------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.16"

  name = local.name
  cidr = var.vpc_cidr
  azs  = local.azs

  private_subnets = [for i in range(2) : cidrsubnet(var.vpc_cidr, 4, i)]
  public_subnets  = [for i in range(2) : cidrsubnet(var.vpc_cidr, 4, i + 8)]
  database_subnets = [for i in range(2) : cidrsubnet(var.vpc_cidr, 4, i + 12)]

  # One NAT gateway rather than one per AZ. Provider APIs are reached through
  # it, and losing it costs a run rather than data -- checkpoints are in RDS,
  # which is not behind NAT. Per-AZ NAT would roughly double network cost for
  # availability the workload can already recover from.
  enable_nat_gateway     = true
  single_nat_gateway     = true
  one_nat_gateway_per_az = false

  enable_dns_hostnames = true
  enable_dns_support   = true

  # Flow logs to CloudWatch. The egress NetworkPolicy blocks the metadata
  # endpoint, and flow logs are how you find out whether anything tried.
  enable_flow_log                                 = true
  create_flow_log_cloudwatch_log_group            = true
  create_flow_log_cloudwatch_iam_role             = true
  flow_log_max_aggregation_interval               = 60
  flow_log_cloudwatch_log_group_retention_in_days = 14

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

# --- cluster ---------------------------------------------------------------

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.31"

  cluster_name    = local.name
  cluster_version = var.kubernetes_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Private endpoint plus a CIDR allowlist for the public one. A fully public
  # API server is reachable from the internet by anyone with a valid token, and
  # tokens leak.
  cluster_endpoint_private_access      = true
  cluster_endpoint_public_access       = true
  cluster_endpoint_public_access_cidrs = var.api_allowed_cidrs

  # Audit logs are the only record of who did what to the cluster. Enabling
  # them after an incident does not help with that incident.
  cluster_enabled_log_types = ["api", "audit", "authenticator"]

  # Secrets encrypted with a customer-managed KMS key. Kubernetes Secrets are
  # base64, not encrypted, and etcd at rest without this is plaintext to
  # anyone who reaches it.
  create_kms_key            = true
  kms_key_enable_default_policy = true
  cluster_encryption_config = { resources = ["secrets"] }

  enable_irsa = true

  eks_managed_node_groups = {
    # On-demand baseline for the control plane and Redis. An interrupted
    # control plane is a blip; an interrupted Redis is a quota-coordination gap
    # (ADR-011), so neither belongs on Spot.
    system = {
      instance_types = ["t4g.medium"]
      ami_type       = "AL2023_ARM_64_STANDARD"
      capacity_type  = "ON_DEMAND"
      min_size       = 2
      max_size       = 4
      desired_size   = 2
      labels         = { workload = "system" }
    }

    # Spot for run pods. Runs are checkpointed and interruption-tolerant by
    # construction -- that IS the product claim -- so refusing Spot would be an
    # odd lack of confidence in it. A Spot interruption is another chaos event.
    runs = {
      instance_types = ["t4g.large", "t4g.xlarge", "m6g.large"]
      ami_type       = "AL2023_ARM_64_STANDARD"
      capacity_type  = "SPOT"
      min_size       = 0
      max_size       = 10
      desired_size   = 1
      labels         = { workload = "runs" }
      taints = [{
        # Taint so only run pods land here and tolerate the interruption.
        key    = "workload"
        value  = "runs"
        effect = "NO_SCHEDULE"
      }]
    }
  }
}

# --- database --------------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = module.vpc.database_subnets
}

resource "aws_security_group" "database" {
  name        = "${local.name}-db"
  description = "Postgres, reachable only from cluster nodes"
  vpc_id      = module.vpc.vpc_id

  ingress {
    description     = "Postgres from the cluster"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [module.eks.node_security_group_id]
  }

  # No egress rule: the database initiates nothing. An unrestricted egress rule
  # on a database is a data-exfiltration path with no upside.
}

resource "random_password" "database" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "database" {
  name                    = "swarmd/${var.environment}/database"
  # Zero-day recovery so a torn-down dev environment can be recreated with the
  # same secret name immediately. Production keeps the default window.
  recovery_window_in_days = var.environment == "prod" ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "database" {
  secret_id = aws_secretsmanager_secret.database.id
  secret_string = jsonencode({
    url = join("", [
      "postgres://swarmd:", random_password.database.result,
      "@", aws_db_instance.main.address, ":5432/swarmd",
    ])
  })
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "16"
  # db.t4g.medium is ample: this database is small and write-light. It holds
  # checkpoints, the ledger, approvals, the skill library and frozen criteria
  # -- durable, not hot.
  instance_class = var.db_instance_class

  allocated_storage     = 20
  max_allocated_storage = 100
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "swarmd"
  username = "swarmd"
  password = random_password.database.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]

  # Multi-AZ in prod. An approval queue that loses a human decision loses the
  # audit trail, and an audit trail that can be approximately reconstructed is
  # not an audit trail.
  multi_az = var.environment == "prod"

  backup_retention_period = var.environment == "prod" ? 14 : 1
  backup_window           = "03:00-04:00"
  maintenance_window       = "sun:04:00-sun:05:00"

  # Deletion protection and a final snapshot in prod. The one time this matters
  # is a mistaken `terraform destroy`, and by then it is too late to add.
  deletion_protection       = var.environment == "prod"
  skip_final_snapshot       = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${local.name}-final" : null

  performance_insights_enabled = var.environment == "prod"
  auto_minor_version_upgrade   = true
}

# --- artifact storage ------------------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  bucket = "${local.name}-artifacts"
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  # Versioned because ledgers are append-only evidence. An overwrite that
  # cannot be recovered would make the ledger's immutability claim depend on
  # nobody making a mistake.
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "archive-old-ledgers"
    status = "Enabled"
    filter {}

    # Ledgers are write-once, read-rarely, keep-forever. Glacier at 90 days is
    # the shape of that access pattern.
    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }
    noncurrent_version_expiration { noncurrent_days = 365 }
  }
}

# --- registry --------------------------------------------------------------

resource "aws_ecr_repository" "backend" {
  name                 = "swarmd"
  image_tag_mutability = "IMMUTABLE" # a tag that can be repointed is not a pin
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_repository" "frontend" {
  name                 = "swarmd-frontend"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

# --- provider credentials --------------------------------------------------

resource "aws_secretsmanager_secret" "providers" {
  name                    = "swarmd/${var.environment}/providers"
  description             = "LLM provider API keys, synced into the cluster by External Secrets"
  recovery_window_in_days = var.environment == "prod" ? 30 : 0
}

# Deliberately no aws_secretsmanager_secret_version here. Terraform state would
# hold the plaintext keys, and state is a file people copy around. Keys are set
# out of band:
#   aws secretsmanager put-secret-value --secret-id swarmd/prod/providers \
#     --secret-string '{"groq_api_key":"...","google_api_key":"..."}'

# --- IRSA ------------------------------------------------------------------

data "aws_iam_policy_document" "external_secrets_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [module.eks.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${module.eks.oidc_provider}:sub"
      values   = ["system:serviceaccount:swarmd:swarmd"]
    }
  }
}

resource "aws_iam_role" "external_secrets" {
  name               = "${local.name}-external-secrets"
  assume_role_policy = data.aws_iam_policy_document.external_secrets_assume.json
}

data "aws_iam_policy_document" "external_secrets" {
  # Read only the two secrets this project owns. A wildcard here would let a
  # compromised pod read every secret in the account.
  statement {
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [
      aws_secretsmanager_secret.providers.arn,
      aws_secretsmanager_secret.database.arn,
    ]
  }
  statement {
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.artifacts.arn}/*"]
  }
}

resource "aws_iam_role_policy" "external_secrets" {
  role   = aws_iam_role.external_secrets.id
  policy = data.aws_iam_policy_document.external_secrets.json
}

# --- budget guard ----------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Alert at 80% of forecast, not only at 100% of actual. By the time actual
  # spend crosses the limit the money is already gone; a forecast breach is
  # the only warning that arrives in time to act on.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_alert_emails
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.budget_alert_emails
  }
}
