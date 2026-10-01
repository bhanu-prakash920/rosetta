output "vpc_id" {
  description = "Id of the VPC."
  value       = aws_vpc.this.id
}

output "vpc_cidr" {
  description = "Address range of the VPC."
  value       = aws_vpc.this.cidr_block
}

output "public_subnet_ids" {
  description = "Subnets for load balancers."
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Subnets for Kubernetes nodes."
  value       = aws_subnet.private[*].id
}

output "data_subnet_ids" {
  description = "Subnets for Kafka, PostgreSQL and Redis. No route to the internet."
  value       = aws_subnet.data[*].id
}
