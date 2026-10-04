output "primary_endpoint" {
  description = "Host name of the primary node."
  value       = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "url_secret_arn" {
  description = "Secrets Manager secret holding the complete Redis URL."
  value       = aws_secretsmanager_secret.url.arn
}

output "security_group_id" {
  description = "Security group of Redis."
  value       = aws_security_group.this.id
}
