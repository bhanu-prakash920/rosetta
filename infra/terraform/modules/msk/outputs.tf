output "cluster_arn" {
  description = "ARN of the cluster."
  value       = aws_msk_cluster.this.arn
}

output "bootstrap_brokers" {
  description = "Plain-text bootstrap brokers, host:port separated by commas."
  value       = aws_msk_cluster.this.bootstrap_brokers
}

output "bootstrap_brokers_tls" {
  description = "TLS bootstrap brokers, host:port separated by commas."
  value       = aws_msk_cluster.this.bootstrap_brokers_tls
}

output "security_group_id" {
  description = "Security group of the brokers."
  value       = aws_security_group.this.id
}
