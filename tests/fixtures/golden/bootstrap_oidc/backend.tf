terraform {
  backend "s3" {
    bucket         = "golden-tf-state"
    key            = "golden/bootstrap.tfstate"
    region         = "us-west-2"
    dynamodb_table = "golden-locks"
  }
}
