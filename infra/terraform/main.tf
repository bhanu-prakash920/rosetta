# Rosetta on AWS: the infrastructure underneath the Helm chart.
#
#   VPC          three tiers of subnets: public (load balancers, NAT), private
#                (Kubernetes nodes), data (Kafka, PostgreSQL, Redis, no route to
#                the internet)
#   EKS          the cluster the chart in infra/helm/rosetta is installed on
#   MSK          Kafka
#   RDS          PostgreSQL 16
#   ElastiCache  Redis
#   S3           the Parquet archive
#   KMS          one customer-managed key, used by every store above
#
# Nothing in here is reachable from the internet except, optionally, the
# Kubernetes API, and that is off by default.
#
# THIS CONFIGURATION HAS NEVER BEEN APPLIED. Read README.md before you rely on it.

data "aws_caller_identity" "current" {}

module "kms" {
  source = "./modules/kms"

  name        = local.name
  description = "Encryption at rest for ${local.name}: RDS, MSK, ElastiCache, S3, EKS secrets, logs"
  tags        = local.tags
}

module "vpc" {
  source = "./modules/vpc"

  name               = local.name
  cidr               = var.vpc_cidr
  azs                = local.azs
  single_nat_gateway = var.single_nat_gateway
  cluster_name       = local.name
  kms_key_arn        = module.kms.key_arn
  log_retention_days = var.log_retention_days
  tags               = local.tags
}

module "s3" {
  source = "./modules/s3"

  bucket_name     = var.archive_bucket_name != "" ? var.archive_bucket_name : "${local.name}-telemetry-${data.aws_caller_identity.current.account_id}"
  kms_key_arn     = module.kms.key_arn
  expiration_days = var.archive_expiration_days
  force_destroy   = !var.deletion_protection
  tags            = local.tags
}

module "eks" {
  source = "./modules/eks"

  name                         = local.name
  kubernetes_version           = var.kubernetes_version
  subnet_ids                   = module.vpc.private_subnet_ids
  endpoint_public_access       = var.cluster_endpoint_public_access
  endpoint_public_access_cidrs = var.cluster_endpoint_public_access_cidrs
  kms_key_arn                  = module.kms.key_arn
  log_retention_days           = var.log_retention_days
  node_instance_types          = var.node_instance_types
  node_min_size                = var.node_min_size
  node_max_size                = var.node_max_size
  node_desired_size            = var.node_desired_size
  workload_namespace           = var.kubernetes_namespace
  workload_service_account     = var.kubernetes_service_account
  workload_policy_arns         = { archive = module.s3.access_policy_arn }
  tags                         = local.tags
}

module "msk" {
  source = "./modules/msk"

  name                     = local.name
  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.data_subnet_ids
  client_security_groups   = { eks = module.eks.node_security_group_id }
  kafka_version            = var.kafka_version
  instance_type            = var.kafka_instance_type
  broker_count             = var.kafka_brokers_per_az * var.az_count
  volume_size_gb           = var.kafka_volume_size_gb
  client_broker_encryption = var.kafka_client_broker_encryption
  kms_key_arn              = module.kms.key_arn
  log_retention_days       = var.log_retention_days
  tags                     = local.tags
}

module "rds" {
  source = "./modules/rds"

  name                     = local.name
  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.data_subnet_ids
  client_security_groups   = { eks = module.eks.node_security_group_id }
  engine_version           = var.postgres_engine_version
  instance_class           = var.postgres_instance_class
  allocated_storage_gb     = var.postgres_allocated_storage_gb
  max_allocated_storage_gb = var.postgres_max_allocated_storage_gb
  multi_az                 = var.postgres_multi_az
  backup_retention_days    = var.postgres_backup_retention_days
  deletion_protection      = var.deletion_protection
  kms_key_arn              = module.kms.key_arn
  tags                     = local.tags
}

module "elasticache" {
  source = "./modules/elasticache"

  name                   = local.name
  vpc_id                 = module.vpc.vpc_id
  subnet_ids             = module.vpc.data_subnet_ids
  client_security_groups = { eks = module.eks.node_security_group_id }
  engine_version         = var.redis_engine_version
  node_type              = var.redis_node_type
  node_count             = var.redis_replicas
  kms_key_arn            = module.kms.key_arn
  tags                   = local.tags
}
