output "region" {
  description = "AWS region."
  value       = var.region
}

output "vpc_id" {
  description = "Id of the VPC."
  value       = module.vpc.vpc_id
}

output "cluster_name" {
  description = "Name of the EKS cluster: aws eks update-kubeconfig --name <this>."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "Endpoint of the Kubernetes API."
  value       = module.eks.cluster_endpoint
}

output "workload_role_arn" {
  description = "IAM role for the Rosetta ServiceAccount: serviceAccount.annotations of the chart."
  value       = module.eks.workload_role_arn
}

output "kafka_bootstrap_brokers" {
  description = "Plain-text bootstrap brokers: config.ROSETTA_KAFKA_BOOTSTRAP of the chart. Empty when the plain-text listener is off."
  value       = module.msk.bootstrap_brokers
}

output "kafka_bootstrap_brokers_tls" {
  description = "TLS bootstrap brokers."
  value       = module.msk.bootstrap_brokers_tls
}

output "postgres_endpoint" {
  description = "Host and port of the database."
  value       = module.rds.endpoint
}

output "postgres_database" {
  description = "Name of the database."
  value       = module.rds.database_name
}

output "postgres_master_secret_arn" {
  description = "Secrets Manager secret with the master user name and password, managed and rotated by RDS."
  value       = module.rds.master_user_secret_arn
}

output "redis_endpoint" {
  description = "Primary endpoint of Redis. TLS is required: use the rediss scheme."
  value       = module.elasticache.primary_endpoint
}

output "redis_url_secret_arn" {
  description = "Secrets Manager secret holding the complete ROSETTA_REDIS_URL."
  value       = module.elasticache.url_secret_arn
}

output "archive_bucket" {
  description = "Bucket of the Parquet archive: config.ROSETTA_S3_BUCKET of the chart."
  value       = module.s3.bucket_name
}

output "kms_key_arn" {
  description = "Key that encrypts every store."
  value       = module.kms.key_arn
}

output "helm_values" {
  description = "Non-secret values for the Helm chart, ready for a values file."
  value = {
    config = {
      ROSETTA_KAFKA_BOOTSTRAP = module.msk.bootstrap_brokers
      ROSETTA_S3_ENDPOINT     = "https://s3.${var.region}.amazonaws.com"
      ROSETTA_S3_BUCKET       = module.s3.bucket_name
      AWS_DEFAULT_REGION      = var.region
      # Amazon RDS does not offer the TimescaleDB extension
      ROSETTA_TELEMETRY_STORE = "parquet"
    }
    serviceAccount = {
      name = var.kubernetes_service_account
      annotations = {
        "eks.amazonaws.com/role-arn" = module.eks.workload_role_arn
      }
    }
  }
}
