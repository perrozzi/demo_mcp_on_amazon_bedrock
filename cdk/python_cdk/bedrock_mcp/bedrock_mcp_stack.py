import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_elasticloadbalancingv2 as elbv2,
    aws_autoscaling as autoscaling,
    Stack
)
from constructs import Construct


class BedrockMcpStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        name_prefix = kwargs.get('name_prefix', 'MCP')
        prefix = name_prefix

        # Create VPC
        vpc = ec2.Vpc(
            self, f"{prefix}-VPC",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24
                )
            ]
        )

        # Create Security Group
        sg = ec2.SecurityGroup(
            self, f"{prefix}-SG",
            vpc=vpc,
            allow_all_outbound=True,
            description="Security group for MCP services"
        )

        sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.tcp(8502),
            "Streamlit UI"
        )

        # Create IAM Role
        role = iam.Role(
            self, "EC2-Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")
            ]
        )

        role.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "bedrock:InvokeModel*",
                "bedrock:ListFoundationModels"
            ],
            resources=["*"]
        ))

        # Create Application Load Balancer
        alb = elbv2.ApplicationLoadBalancer(
            self, f"{prefix}-ALB",
            vpc=vpc,
            internet_facing=True
        )

        # Create ALB Listeners
        streamlit_listener = alb.add_listener(
            "Streamlit",
            port=8502,
            protocol=elbv2.ApplicationProtocol.HTTP
        )

        # Create IAM User for API Access with dynamic name
        api_user = iam.User(
            self, "BedrockApiUser",
            user_name=f"bedrock-mcp-api-user-{Stack.of(self).stack_name}"
        )

        api_user.add_to_policy(iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=[
                "bedrock:InvokeModel*",
                "bedrock:ListFoundationModels"
            ],
            resources=["*"]
        ))

        # Create access key for the user
        access_key = iam.CfnAccessKey(
            self, "BedrockApiAccessKey",
            user_name=api_user.user_name
        )

        # Create User Data with improved initialization
        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            '#!/bin/bash',
            
            # Set HOME and PATH environment variables first
            'export HOME=/root',
            'export PATH="/usr/local/bin:$PATH"',
            
            # Update and install dependencies
            'apt-get update',
            'apt-get install -y software-properties-common',
            'add-apt-repository -y ppa:deadsnakes/ppa',
            'apt-get update',
            'apt-get install -y python3.12 python3.12-venv git',
            
            # Install Node.js
            'curl -fsSL https://deb.nodesource.com/setup_22.x | bash -',
            'apt-get install -y nodejs',
            
            # Install UV for ubuntu user
            'su - ubuntu -c "curl -LsSf https://astral.sh/uv/install.sh | sh"',
            'echo \'export PATH="/home/ubuntu/.local/bin:$PATH"\' >> /home/ubuntu/.bashrc',
            
            # Create and set up project directory with proper ownership
            'mkdir -p /home/ubuntu/demo_mcp_on_amazon_bedrock',
            'chown ubuntu:ubuntu /home/ubuntu/demo_mcp_on_amazon_bedrock',
            'cd /home/ubuntu/demo_mcp_on_amazon_bedrock',
            
            # Clone project with HTTPS and retry logic
            'MAX_RETRIES=3',
            'RETRY_COUNT=0',
            'while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do',
            '    git clone https://github.com/aws-samples/demo_mcp_on_amazon_bedrock.git . && break',
            '    RETRY_COUNT=$((RETRY_COUNT+1))',
            '    if [ $RETRY_COUNT -lt $MAX_RETRIES ]; then',
            '        echo "Git clone attempt $RETRY_COUNT failed, retrying in 5 seconds..."',
            '        sleep 5',
            '    fi',
            'done',
            
            # Exit if git clone ultimately failed
            '[ -z "$(ls -A /home/ubuntu/demo_mcp_on_amazon_bedrock)" ] && echo "Failed to clone repository" && exit 1',
            
            # Create necessary directories with proper ownership
            'mkdir -p logs tmp',
            'chown -R ubuntu:ubuntu /home/ubuntu/demo_mcp_on_amazon_bedrock',
            'chmod 755 /home/ubuntu/demo_mcp_on_amazon_bedrock',
            'chmod 755 logs tmp',

            # Setup Python environment as ubuntu user
            'su - ubuntu -c "cd /home/ubuntu/demo_mcp_on_amazon_bedrock && \
                python3.12 -m venv .venv && \
                source .venv/bin/activate && \
                source /home/ubuntu/.bashrc && \
                uv pip install ."',

            # Configure environment with proper ownership
            'cat > .env << EOL',
            f'AWS_ACCESS_KEY_ID={access_key.ref}',
            f'AWS_SECRET_ACCESS_KEY={access_key.attr_secret_access_key}',
            f'AWS_REGION={Stack.of(self).region}',
            'LOG_DIR=./logs',
            'CHATBOT_SERVICE_PORT=8502',
            'MCP_SERVICE_HOST=127.0.0.1',
            'MCP_SERVICE_PORT=7002',
            f'API_KEY={cdk.Names.unique_id(self)}',
            'EOL',
            'chown ubuntu:ubuntu .env',
            'chmod 600 .env',  # Secure permissions for credentials file
            
            # Setup systemd service
            'cat > /etc/systemd/system/mcp-services.service << EOL',
            '[Unit]',
            'Description=MCP Services',
            'After=network.target',
            '',
            '[Service]',
            'Type=forking',
            'User=ubuntu',
            'Environment="HOME=/home/ubuntu"',
            'Environment="PATH=/home/ubuntu/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"',
            'WorkingDirectory=/home/ubuntu/demo_mcp_on_amazon_bedrock',
            'ExecStart=/bin/bash start_all.sh',
            'ExecStop=/bin/bash stop_all.sh',
            'Restart=always',
            '',
            '[Install]',
            'WantedBy=multi-user.target',
            'EOL',
            
            # Enable and start service
            'systemctl daemon-reload',
            'systemctl enable mcp-services',
            'systemctl start mcp-services'
        )

        # Create Auto Scaling Group
        asg = autoscaling.AutoScalingGroup(
            self, f"{prefix}-ASG",
            vpc=vpc,
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.T3, ec2.InstanceSize.MEDIUM),
            machine_image=ec2.MachineImage.from_ssm_parameter(
                '/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id',
                os=ec2.OperatingSystemType.LINUX
            ),
            block_devices=[
                autoscaling.BlockDevice(
                    device_name='/dev/sda1',  # Root volume
                    volume=autoscaling.BlockDeviceVolume.ebs(100)  # 100 GB
                )
            ],
            user_data=user_data,
            role=role,
            security_group=sg,
            min_capacity=1,
            max_capacity=1,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            )
        )

        # Add ASG as target for ALB listeners
        streamlit_listener.add_targets(
            "Streamlit-Target",
            port=8502,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[asg],
            health_check=elbv2.HealthCheck(
                path='/',
                unhealthy_threshold_count=2,
                healthy_threshold_count=5,
                interval=cdk.Duration.seconds(30)
            )
        )

        # Stack Outputs
        cdk.CfnOutput(
            self, "Streamlit-Endpoint",
            value=f"http://{alb.load_balancer_dns_name}:8502",
            description="Streamlit UI Endpoint"
        )

        # Output the API credentials
        cdk.CfnOutput(
            self, "ApiAccessKeyId",
            value=access_key.ref,
            description="API Access Key ID"
        )

        cdk.CfnOutput(
            self, "ApiSecretAccessKey",
            value=access_key.attr_secret_access_key,
            description="API Secret Access Key"
        )
