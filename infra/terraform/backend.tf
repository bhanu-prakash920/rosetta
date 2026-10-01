# Remote state. Left as a commented example on purpose: the bucket and the lock
# table have to exist before `terraform init`, and they are not created here.
#
# The state of this configuration contains the Redis AUTH token (see
# modules/elasticache), so it must be stored encrypted and with restricted
# access. Do not keep it on a laptop and never commit it.
#
# terraform {
#   backend "s3" {
#     bucket         = "example-terraform-state"
#     key            = "rosetta/production/terraform.tfstate"
#     region         = "eu-west-1"
#     encrypt        = true
#     kms_key_id     = "alias/terraform-state"
#     dynamodb_table = "terraform-locks"
#   }
# }
