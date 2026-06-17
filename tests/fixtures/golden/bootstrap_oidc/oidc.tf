# Adopt the existing account-global GitHub Actions OIDC provider (CI already
# assumes it). Set create_oidc_provider: true to have rc create it instead.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}
