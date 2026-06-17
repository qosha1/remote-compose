data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# The role CI assumes via GitHub OIDC. Trust is scoped to the configured repo +
# branch; permissions are derived from rc.yml bootstrap.permissions.
resource "aws_iam_role" "deploy" {
  name               = "golden-github-deploy"
  assume_role_policy = <<EOT
{
  "Statement": [
    {
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:acme/app:ref:refs/heads/main"
        }
      },
      "Effect": "Allow",
      "Principal": {
        "Federated": "${data.aws_iam_openid_connect_provider.github.arn}"
      }
    }
  ],
  "Version": "2012-10-17"
}
EOT
}

resource "aws_iam_role_policy" "deploy" {
  name = "golden-github-deploy-permissions"
  role = aws_iam_role.deploy.id

  policy = <<EOT
{
  "Statement": [
    {
      "Action": [
        "codebuild:StartBuild",
        "codebuild:StartBuildBatch",
        "codebuild:BatchGetBuilds",
        "codebuild:BatchGetBuildBatches"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:codebuild:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:project/golden-build",
      "Sid": "CodeBuildDeploy"
    },
    {
      "Action": [
        "ecr:GetAuthorizationToken"
      ],
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "EcrAuth"
    },
    {
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage",
        "ecr:PutImage",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:DescribeRepositories",
        "ecr:CreateRepository"
      ],
      "Effect": "Allow",
      "Resource": "arn:aws:ecr:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:repository/golden/*",
      "Sid": "EcrPushPull"
    },
    {
      "Action": [
        "ecs:UpdateService",
        "ecs:DescribeServices",
        "ecs:ListServices",
        "ecs:DescribeTasks",
        "ecs:ListTasks",
        "ecs:DescribeClusters"
      ],
      "Effect": "Allow",
      "Resource": [
        "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:service/golden-cluster/*",
        "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/golden-cluster",
        "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:service/foundry-tenant-*/*",
        "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:cluster/foundry-tenant-*"
      ],
      "Sid": "EcsDeployServices"
    },
    {
      "Action": [
        "ecs:RegisterTaskDefinition",
        "ecs:DeregisterTaskDefinition",
        "ecs:DescribeTaskDefinition",
        "ecs:ListTaskDefinitions"
      ],
      "Effect": "Allow",
      "Resource": "*",
      "Sid": "EcsTaskDefinitions"
    },
    {
      "Action": [
        "iam:PassRole"
      ],
      "Condition": {
        "StringEquals": {
          "iam:PassedToService": "ecs-tasks.amazonaws.com"
        }
      },
      "Effect": "Allow",
      "Resource": [
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/golden-task",
        "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/golden-task-exec"
      ],
      "Sid": "PassTaskRoles"
    }
  ],
  "Version": "2012-10-17"
}
EOT
}

