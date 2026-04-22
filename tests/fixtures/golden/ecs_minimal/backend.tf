terraform {
  backend "s3" {
    bucket = "golden-tf-state"
    key    = "golden/ecs.tfstate"
    region = "us-west-2"
  }
}

