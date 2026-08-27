variable "environment" {
  description = "Environment name. Drives Multi-AZ, backup retention, deletion protection."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "VPC CIDR. /16 leaves room for subnets without renumbering later."
  type        = string
  default     = "10.42.0.0/16"
}

variable "kubernetes_version" {
  description = "EKS control plane version. Pinned rather than latest: an unplanned control-plane upgrade during a run is not a thing anyone wants to debug."
  type        = string
  default     = "1.31"
}

variable "api_allowed_cidrs" {
  description = "CIDRs permitted to reach the public API server endpoint. Defaults to nothing usable on purpose - an open API server is reachable by anyone holding a leaked token."
  type        = list(string)
  default     = ["127.0.0.1/32"]
}

variable "db_instance_class" {
  description = "RDS instance class. This database is small and write-light: it holds checkpoints, the ledger, approvals and criteria, so it needs durability rather than throughput."
  type        = string
  default     = "db.t4g.medium"
}

variable "monthly_budget_usd" {
  description = "Monthly AWS budget. Infrastructure is ~$280/month against ~$0 of LLM spend; the headroom catches a misconfiguration, not normal operation."
  type        = string
  default     = "400"
}

variable "budget_alert_emails" {
  description = "Where budget alerts go. Empty means nobody is told, which defeats the budget."
  type        = list(string)
  default     = []
}
