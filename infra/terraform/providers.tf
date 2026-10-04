provider "aws" {
  region = var.region

  # every resource that supports tags gets these
  default_tags {
    tags = local.tags
  }
}
