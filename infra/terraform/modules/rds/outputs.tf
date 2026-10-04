output "endpoint" {
  description = "Host and port."
  value       = aws_db_instance.this.endpoint
}

output "address" {
  description = "Host name."
  value       = aws_db_instance.this.address
}

output "database_name" {
  description = "Name of the database."
  value       = aws_db_instance.this.db_name
}

output "master_user_secret_arn" {
  description = "Secrets Manager secret with the master user name and password."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "security_group_id" {
  description = "Security group of the database."
  value       = aws_security_group.this.id
}
