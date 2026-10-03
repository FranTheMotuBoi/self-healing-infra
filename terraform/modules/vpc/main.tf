# terraform/modules/vpc/main.tf
# Production VPC: 3 AZs, public + private subnets, NAT Gateway, VPC Flow Logs

data "aws_availability_zones" "available" { state = "available" }

locals {
  azs             = slice(data.aws_availability_zones.available.names, 0, 3)
  public_cidrs    = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i)]
  private_cidrs   = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i + 3)]
  db_cidrs        = [for i, _ in local.azs : cidrsubnet(var.vpc_cidr, 4, i + 6)]
}

# ── VPC ──────────────────────────────────────────────────────────
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = merge(var.tags, {
    Name = "${var.project_name}-vpc-${var.environment}"
  })
}

# ── Internet Gateway ──────────────────────────────────────────────
resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(var.tags, { Name = "${var.project_name}-igw-${var.environment}" })
}

# ── Public Subnets ────────────────────────────────────────────────
resource "aws_subnet" "public" {
  count                   = length(local.azs)
  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_cidrs[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false  # No auto-assign; ALB uses specific IPs

  tags = merge(var.tags, {
    Name = "${var.project_name}-public-${local.azs[count.index]}-${var.environment}"
    Tier = "public"
    "kubernetes.io/role/elb" = "1"  # EKS compatibility
  })
}

# ── Private App Subnets ───────────────────────────────────────────
resource "aws_subnet" "private" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = merge(var.tags, {
    Name = "${var.project_name}-private-${local.azs[count.index]}-${var.environment}"
    Tier = "private"
    "kubernetes.io/role/internal-elb" = "1"
  })
}

# ── Private DB Subnets ────────────────────────────────────────────
resource "aws_subnet" "database" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.db_cidrs[count.index]
  availability_zone = local.azs[count.index]

  tags = merge(var.tags, {
    Name = "${var.project_name}-db-${local.azs[count.index]}-${var.environment}"
    Tier = "database"
  })
}

# ── Elastic IPs for NAT ───────────────────────────────────────────
resource "aws_eip" "nat" {
  count  = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : length(local.azs)) : 0
  domain = "vpc"
  tags   = merge(var.tags, { Name = "${var.project_name}-nat-eip-${count.index}-${var.environment}" })

  depends_on = [aws_internet_gateway.main]
}

# ── NAT Gateway ───────────────────────────────────────────────────
resource "aws_nat_gateway" "main" {
  count         = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : length(local.azs)) : 0
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(var.tags, {
    Name = "${var.project_name}-nat-${count.index}-${var.environment}"
  })

  depends_on = [aws_internet_gateway.main]
}

# ── Route Tables ──────────────────────────────────────────────────
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = merge(var.tags, { Name = "${var.project_name}-public-rt-${var.environment}" })
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  count  = var.enable_nat_gateway ? (var.single_nat_gateway ? 1 : length(local.azs)) : 1
  vpc_id = aws_vpc.main.id

  dynamic "route" {
    for_each = var.enable_nat_gateway ? [1] : []
    content {
      cidr_block     = "0.0.0.0/0"
      nat_gateway_id = var.single_nat_gateway ? aws_nat_gateway.main[0].id : aws_nat_gateway.main[count.index].id
    }
  }

  tags = merge(var.tags, { Name = "${var.project_name}-private-rt-${count.index}-${var.environment}" })
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = var.single_nat_gateway ? aws_route_table.private[0].id : aws_route_table.private[count.index].id
}

resource "aws_route_table_association" "database" {
  count          = length(aws_subnet.database)
  subnet_id      = aws_subnet.database[count.index].id
  route_table_id = var.single_nat_gateway ? aws_route_table.private[0].id : aws_route_table.private[min(count.index, length(aws_route_table.private) - 1)].id
}

# ── VPC Flow Logs ─────────────────────────────────────────────────
resource "aws_flow_log" "main" {
  count                = var.enable_flow_logs ? 1 : 0
  iam_role_arn         = aws_iam_role.flow_log[0].arn
  log_destination      = aws_cloudwatch_log_group.flow_log[0].arn
  traffic_type         = "ALL"
  vpc_id               = aws_vpc.main.id
  max_aggregation_interval = 60

  tags = merge(var.tags, { Name = "${var.project_name}-flow-log-${var.environment}" })
}

resource "aws_cloudwatch_log_group" "flow_log" {
  count             = var.enable_flow_logs ? 1 : 0
  name              = "/aws/vpc/flowlogs/${var.project_name}-${var.environment}"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_iam_role" "flow_log" {
  count = var.enable_flow_logs ? 1 : 0
  name  = "${var.project_name}-flow-log-role-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow",
      Principal = { Service = "vpc-flow-logs.amazonaws.com" } }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "flow_log" {
  count = var.enable_flow_logs ? 1 : 0
  name  = "flow-log-policy"
  role  = aws_iam_role.flow_log[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["logs:CreateLogGroup", "logs:CreateLogStream",
                "logs:PutLogEvents", "logs:DescribeLogGroups",
                "logs:DescribeLogStreams"]
      Resource = "*"
    }]
  })
}

# ── VPC Endpoints (reduce NAT costs for AWS services) ────────────
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id
  tags = merge(var.tags, { Name = "${var.project_name}-s3-endpoint-${var.environment}" })
}

resource "aws_vpc_endpoint" "ssm" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ssm"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
  tags = merge(var.tags, { Name = "${var.project_name}-ssm-endpoint-${var.environment}" })
}

resource "aws_vpc_endpoint" "ssmmessages" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.aws_region}.ssmmessages"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
  tags = merge(var.tags, { Name = "${var.project_name}-ssmmessages-endpoint-${var.environment}" })
}

resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.project_name}-vpc-endpoints-sg-${var.environment}"
  description = "Allow HTTPS from within the VPC to VPC endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
  tags = merge(var.tags, { Name = "${var.project_name}-vpc-endpoints-sg-${var.environment}" })
}

# ── Outputs ───────────────────────────────────────────────────────
output "vpc_id"             { value = aws_vpc.main.id }
output "vpc_cidr"           { value = aws_vpc.main.cidr_block }
output "public_subnet_ids"  { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "database_subnet_ids"{ value = aws_subnet.database[*].id }
output "nat_gateway_ips"    { value = aws_eip.nat[*].public_ip }
