# terraform/modules/monitoring/variables.tf

variable "project_name" { type = string }
variable "environment" { type = string }
variable "alert_emails" { type = list(string); default = [] }
variable "alb_arn_suffix" { type = string }
variable "target_group_arn_suffix" { type = string }
variable "asg_name" { type = string }
variable "rds_identifier" { type = string; default = "" }
variable "healer_lambda_arn" { type = string }
variable "health_checker_lambda_arn" { type = string }
variable "tags" { type = map(string); default = {} }

# terraform/modules/monitoring/outputs.tf
output "sns_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
