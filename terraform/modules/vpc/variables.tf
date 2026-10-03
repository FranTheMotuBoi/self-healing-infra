# terraform/modules/vpc/variables.tf
variable "project_name"       { type = string }
variable "environment"        { type = string }
variable "aws_region"         { type = string; default = "us-east-1" }
variable "vpc_cidr"           { type = string; default = "10.0.0.0/16" }
variable "enable_nat_gateway" { type = bool;   default = true }
variable "single_nat_gateway" { type = bool;   default = false  # true saves cost in dev }
variable "enable_flow_logs"   { type = bool;   default = true }
variable "tags"               { type = map(string); default = {} }
