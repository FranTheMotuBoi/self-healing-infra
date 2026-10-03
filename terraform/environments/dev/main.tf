# terraform/environments/dev/main.tf
# Development environment - wires all modules together

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Uncomment to use S3 backend (recommended for teams)
  # backend "s3" {
  #   bucket         = "your-terraform-state-bucket"
  #   key            = "self-healing-infra/dev/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "terraform-state-lock"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = "dev"
      ManagedBy   = "terraform"
      Repo        = "self-healing-infra"
    }
  }
}

# ─── VPC (use existing or create) ────────────────────────────────
# For simplicity, using data sources for an existing VPC
# Replace with a VPC module for greenfield deployments
data "aws_vpc" "main" {
  tags = { Name = "${var.project_name}-vpc-dev" }
}

data "aws_subnets" "private" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.main.id]
  }
  tags = { Tier = "private" }
}

data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.main.id]
  }
  tags = { Tier = "public" }
}

# ─── S3 Bucket for Logs ──────────────────────────────────────────
resource "aws_s3_bucket" "logs" {
  bucket = "${var.project_name}-logs-dev-${data.aws_caller_identity.current.account_id}"

  tags = { Name = "${var.project_name}-logs-dev" }
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    id     = "archive-old-logs"
    status = "Enabled"

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 90
      storage_class = "GLACIER"
    }

    expiration {
      days = 365
    }
  }
}

data "aws_caller_identity" "current" {}

# ─── ALB Module ───────────────────────────────────────────────────
module "alb" {
  source = "../../modules/alb"

  project_name       = var.project_name
  environment        = "dev"
  vpc_id             = data.aws_vpc.main.id
  public_subnet_ids  = data.aws_subnets.public.ids
  app_port           = var.app_port
  health_check_path  = "/health"
  tags               = {}
}

# ─── EC2 / ASG Module ─────────────────────────────────────────────
module "ec2" {
  source = "../../modules/ec2"

  project_name          = var.project_name
  environment           = "dev"
  vpc_id                = data.aws_vpc.main.id
  private_subnet_ids    = data.aws_subnets.private.ids
  alb_security_group_id = module.alb.security_group_id
  target_group_arns     = [module.alb.target_group_arn]
  instance_type         = "t3.micro"
  app_port              = var.app_port
  asg_min_size          = 2
  asg_max_size          = 4
  asg_desired_capacity  = 2
  logs_bucket           = aws_s3_bucket.logs.bucket
  tags                  = {}
}

# ─── Lambda: Health Checker ──────────────────────────────────────
module "lambda_health_checker" {
  source = "../../modules/lambda"

  function_name   = "${var.project_name}-health-checker-dev"
  description     = "Checks health of all services and publishes metrics"
  handler         = "health_checker.lambda_handler"
  runtime         = "python3.11"
  source_dir      = "${path.root}/../../../src"
  timeout         = 30
  memory_size     = 256

  environment_variables = {
    PROJECT_NAME      = var.project_name
    ENVIRONMENT       = "dev"
    ALB_DNS_NAME      = module.alb.dns_name
    ASG_NAME          = module.ec2.asg_name
    SNS_TOPIC_ARN     = module.monitoring.sns_topic_arn
    LOGS_BUCKET       = aws_s3_bucket.logs.bucket
    DRY_RUN           = "false"
    SLACK_WEBHOOK_SSM = "/${var.project_name}/dev/slack-webhook-url"
  }

  tags = {}
}

# ─── Lambda: Auto Healer ─────────────────────────────────────────
module "lambda_healer" {
  source = "../../modules/lambda"

  function_name   = "${var.project_name}-healer-dev"
  description     = "Executes recovery actions in response to failures"
  handler         = "healer.lambda_handler"
  runtime         = "python3.11"
  source_dir      = "${path.root}/../../../src"
  timeout         = 120
  memory_size     = 256

  environment_variables = {
    PROJECT_NAME  = var.project_name
    ENVIRONMENT   = "dev"
    ASG_NAME      = module.ec2.asg_name
    LOGS_BUCKET   = aws_s3_bucket.logs.bucket
    DRY_RUN       = "false"
  }

  tags = {}
}

# ─── Monitoring Module ────────────────────────────────────────────
module "monitoring" {
  source = "../../modules/monitoring"

  project_name                = var.project_name
  environment                 = "dev"
  alert_emails                = var.alert_emails
  alb_arn_suffix              = module.alb.arn_suffix
  target_group_arn_suffix     = module.alb.target_group_arn_suffix
  asg_name                    = module.ec2.asg_name
  rds_identifier              = ""
  healer_lambda_arn           = module.lambda_healer.function_arn
  health_checker_lambda_arn   = module.lambda_health_checker.function_arn
  tags                        = {}
}

# ─── Outputs ─────────────────────────────────────────────────────
output "alb_dns_name" {
  description = "DNS name of the Application Load Balancer"
  value       = module.alb.dns_name
}

output "cloudwatch_dashboard_url" {
  description = "CloudWatch dashboard URL"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${var.project_name}-dev"
}

output "logs_bucket" {
  description = "S3 bucket for logs"
  value       = aws_s3_bucket.logs.bucket
}

output "sns_topic_arn" {
  description = "SNS topic ARN for alerts"
  value       = module.monitoring.sns_topic_arn
}
