output "cluster_name" {
  description = "Name of the cluster."
  value       = aws_eks_cluster.this.name
}

output "cluster_endpoint" {
  description = "Endpoint of the Kubernetes API."
  value       = aws_eks_cluster.this.endpoint
}

output "cluster_certificate_authority" {
  description = "Certificate authority of the cluster, base64."
  value       = aws_eks_cluster.this.certificate_authority[0].data
}

output "node_security_group_id" {
  description = "Security group that EKS attaches to the control plane interfaces and to managed nodes. Pods connect to the data stores from it."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}

output "oidc_provider_arn" {
  description = "OIDC provider for IAM roles for service accounts."
  value       = aws_iam_openid_connect_provider.this.arn
}

output "workload_role_arn" {
  description = "Role for the Rosetta ServiceAccount."
  value       = aws_iam_role.workload.arn
}
