
# ------------------------------------------------------------------
# EFS file systems for persistent compose volumes
# ------------------------------------------------------------------

resource "aws_security_group" "efs" {
  name        = "${var.project}-efs"
  description = "EFS - NFS 2049 from ECS tasks only."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_efs_file_system" "pgdata" {
  creation_token = "${var.project}-pgdata"
  encrypted      = true
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = {
    Name   = "${var.project}-pgdata"
    Volume = "pgdata"
  }
}

resource "aws_efs_mount_target" "pgdata" {
  count           = length(aws_subnet.private)
  file_system_id  = aws_efs_file_system.pgdata.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.efs.id]
}
resource "aws_efs_access_point" "db__pgdata" {
  file_system_id = aws_efs_file_system.pgdata.id

  posix_user {
    uid = 1000
    gid = 1000
  }

  root_directory {
    path = "/db"
    creation_info {
      owner_uid   = 1000
      owner_gid   = 1000
      permissions = "0755"
    }
  }

  tags = {
    Service = "db"
    Volume  = "pgdata"
  }
}
