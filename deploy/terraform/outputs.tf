output "cluster_name" {
  value = module.eks.cluster_name
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "configure_kubectl" {
  description = "Run this to point kubectl at the cluster."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "backend_repository_url" {
  value = aws_ecr_repository.backend.repository_url
}

output "frontend_repository_url" {
  value = aws_ecr_repository.frontend.repository_url
}

output "external_secrets_role_arn" {
  description = "Annotate the swarmd ServiceAccount with this for IRSA."
  value       = aws_iam_role.external_secrets.arn
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "database_secret_name" {
  description = "The connection string lives here, never in Terraform outputs."
  value       = aws_secretsmanager_secret.database.name
}

output "provider_secret_name" {
  description = "Set provider keys out of band; Terraform never holds them."
  value       = aws_secretsmanager_secret.providers.name
}
