terraform {
  required_version = ">= 1.10.0, < 2.0.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = "ap-south-1"
  default_tags {
    tags = {
      Project     = "CodeSPY"
      Environment = "demo"
      ManagedBy   = "OpenTofu"
    }
  }
}
# Setup only: no resources, backend, credentials or deployment permissions.
