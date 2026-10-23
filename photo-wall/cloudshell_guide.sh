# AWS CloudShell Quick-Start Commands for Photo Wall
# You can copy-paste these commands directly into AWS CloudShell in your browser:

# 1. Check your AWS Identity & Region
aws sts get-caller-identity
aws configure get region

# 2. Deploy the Infrastructure via AWS CloudFormation
aws cloudformation deploy \
  --template-file cloudformation_template.yaml \
  --stack-name photo-wall-stack \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides BucketName=photo-wall-simha-p1

# 3. View your created DynamoDB Table
aws dynamodb describe-table --table-name PhotoWallMetadata

# 4. View CloudWatch Logs for Photo Wall
aws logs describe-log-groups --log-group-name-prefix /aws/photowall

# 5. Verify your S3 Bucket
aws s3 ls s3://photo-wall-simha-p1/
