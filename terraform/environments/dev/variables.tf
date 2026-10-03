# terraform/environments/dev/variables.tf

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "self-healing-infra"
}

variable "app_port" {
  description = "Port the application listens on"
  type        = number
  default     = 8080
}

variable "alert_emails" {
  description = "Email addresses to receive alerts"
  type        = list(string)
  default     = []
}
