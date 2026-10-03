# terraform/modules/dynamodb/main.tf
# Circuit breaker + idempotency tables for the self-healing system

resource "aws_dynamodb_table" "circuit_breaker" {
  name         = "${var.project_name}-circuit-breaker-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"   # No capacity planning needed
  hash_key     = "resource_id"

  attribute {
    name = "resource_id"
    type = "S"
  }

  # TTL auto-deletes old entries after 24h
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # Point-in-time recovery
  point_in_time_recovery { enabled = true }

  # Encryption at rest
  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-circuit-breaker-${var.environment}"
    Purpose = "circuit-breaker"
  })
}

resource "aws_dynamodb_table" "idempotency" {
  name         = "${var.project_name}-idempotency-${var.environment}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "resource_id"

  attribute {
    name = "resource_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery { enabled = true }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  tags = merge(var.tags, {
    Name    = "${var.project_name}-idempotency-${var.environment}"
    Purpose = "heal-idempotency"
  })
}

# ── IAM policy for Lambda to access both tables ───────────────────
data "aws_iam_policy_document" "dynamodb_policy" {
  statement {
    effect = "Allow"
    actions = [
      "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
      "dynamodb:DeleteItem", "dynamodb:DescribeTable",
    ]
    resources = [
      aws_dynamodb_table.circuit_breaker.arn,
      aws_dynamodb_table.idempotency.arn,
    ]
  }
}

resource "aws_iam_policy" "dynamodb_access" {
  name        = "${var.project_name}-dynamodb-access-${var.environment}"
  description = "Allows Lambda to use circuit breaker and idempotency tables"
  policy      = data.aws_iam_policy_document.dynamodb_policy.json
  tags        = var.tags
}

output "circuit_breaker_table_name" { value = aws_dynamodb_table.circuit_breaker.name }
output "idempotency_table_name"     { value = aws_dynamodb_table.idempotency.name }
output "dynamodb_policy_arn"        { value = aws_iam_policy.dynamodb_access.arn }
