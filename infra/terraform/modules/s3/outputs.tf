output "bucket_name" {
  description = "Name of the bucket."
  value       = aws_s3_bucket.this.id
}

output "bucket_arn" {
  description = "ARN of the bucket."
  value       = aws_s3_bucket.this.arn
}

output "access_policy_arn" {
  description = "IAM policy that allows reading and writing the archive."
  value       = aws_iam_policy.access.arn
}
