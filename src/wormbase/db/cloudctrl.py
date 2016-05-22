# -*- coding: utf-8 -*-
import base64
import functools
import json
import operator
import os
import pickle
import pprint
import socket
import subprocess
import sys
import time

from botocore.exceptions import ClientError
from botocore.exceptions import ProfileNotFound
from scp import SCPClient
import boto3
import click
import configobj
import paramiko

from .util import echo_error
from .util import echo_info
from .util import echo_retry
from .util import echo_sig
from .util import echo_waiting
from .util import option

BUILD_STATE_PATH = os.path.join(os.getcwd(), '.build-state')

IAM_ASSUME_ROLE_NAME = 'wb-build-db-assume'

IAM_ASSUME_POLICY_NAME = 'wb-build-db-assume'

IAM_DB_BUILD_GROUP = 'wb-db-builders'

IAM_DB_BUILD_ROLE = 'wb-build-db'

KEY_PAIR_PATH = os.getcwd()

LOCAL_ASSUME_ROLE_PROFILE = 'wb-db-builder'

IAM_DB_BUILD_GROUP_POLICIES = {
    'IAMReadOnlyAccess',
}

# XXX: Danger: These names could be changed in the AWS console.
IAM_DB_BUILD_POLICIES = (
    'DecodeAuthorizationMessages',
    'IAMReadOnlyAccess',
    'ec2-manage-instances',
    'ec2-manage-keypairs-and-security-groups',
    'ec2-manage-volumes'
    'ec2-run-db-build-instance',
    'ec2-tagging',
    's3-datomic-backups-full-access',
    'IAMReadOnlyAccess'
)

USER_DATA_TEMPLATE = """#cloud-config
repo_update: true
repo_upgrade: all

packages:
  - openssl-devel
  - readline-devel
  - java-1.8.0-openjdk-headless
  - python34

runcmd:
# until we have Python 3.5, need to get-pip
- wget https://bootstrap.pypa.io/get-pip.py -O /tmp/get-pip.py
- python3 /tmp/get-pip.py
- python3 -m pip --upgrade pip

# hack to make tace work. Expects libreadline.so.5, which is not
# installable
- ln -s /lib64/libreadline.so.6 /lib64/libreadline.so.5

"""

EC2_INSTANCE_DEFAULTS = dict(
    ami='ami-8ff710e2',
    instance_type='t1.micro',
    monitoring=False,
    dry_run=False
)


def _wait_for_sshd(ec2_instance, max_timeout=60 * 6):
    waited = 0
    wait_msg = 'Waiting for connectivity to instance {.id}... '
    wait_msg = wait_msg.format(ec2_instance)
    while True:
        echo_waiting(wait_msg)
        s = socket.socket()
        s.settimeout(20)
        try:
            s.connect((ec2_instance.public_dns_name, 22))
            echo_info('connected')
            break
        except Exception:
            time.sleep(20)
            waited += 40
        if waited >= max_timeout:
            msg = 'Failed to connect via ssh to {.public_dns_name}'
            msg = msg.format(ec2_instance)
            echo_error('Gave up after waiting {} seconds'.format(waited))
            raise socket.timeout(msg)
        else:
            echo_retry('not yet, retrying')


def bootstrap(ec2_instance, package_version):
    """Deploy this package to the AWS instance.

    This involves scp'ing the data due to the repo being private.
    If, in the future this repo is deemed ok to be public,
    then this bootstraping via scp can be eliminated and
    commands run in UserData to fetch and install the data
    directly from a github release.

    This also requires the system package 'python3-dev'.
    """
    shell_cmd = functools.partial(subprocess.Popen, shell=True)
    proc = shell_cmd('python setup.py --fullname', stdout=subprocess.PIPE)
    pkg_fullname = proc.communicate()[0].decode('utf-8').rstrip()
    # Path to filename produced by: python setup.py sdist (for now)
    # Best to download from github release.
    archive_filename = pkg_fullname + '.tar.gz'
    path = os.path.join('dist', archive_filename)
    if not os.path.isfile(path):
        subprocess.check_call('python setup.py sdist', shell=True)
    hostname = ec2_instance.public_dns_name
    priv_key_path = os.path.join(os.getcwd(),
                                 ec2_instance.key_pair.name)
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(hostname,
                key_filename=priv_key_path,
                username='ec2-user',
                timeout=60.0 * 3.5)  # Wait 3½ mins for sshd on ec2 instance
    with SCPClient(ssh.get_transport()) as scp:
        scp.put(path, archive_filename)


def _make_asssume_role_policy(version='2012-10-17', **attrs):
    attrs.setdefault('Effect', 'Allow')
    attrs.setdefault('Action', 'sts:AssumeRole')
    pol_stmt = dict(attrs)
    return dict(Version=version, Statement=[pol_stmt])


def _aws_userid(session):
    """Return a role-id:role_session_name as per AWS documentation.

    See table entry under:
      "Request Information That You Can Use for Policy Variables"

    http://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_variables.html#policy-vars-formoreinfo
    """
    profile_name = session.profile_name
    conf = session._session.full_config['profiles'][profile_name]
    role_name = conf['role_arn'].rsplit('/')[1]
    iam = session.resource('iam')
    role = iam.Role(role_name)
    role.load()
    format_aws_userid = '{role.role_id}:{conf[role_session_name]}'.format
    return format_aws_userid(role=role, conf=conf)


def _aws_session(ctx, profile_name):
    try:
        session = boto3.Session(profile_name=profile_name)
    except ProfileNotFound as pnf:
        echo_error(str(pnf))
        ctx.abort()
    return session


def get_key_pair(ec2, release):
    key_pair_name = 'db-build-{}-keypair'.format(release)
    try:
        key_pair = ec2.KeyPair(key_pair_name)
        key_pair.load()
    except ClientError:
        key_pair = ec2.create_key_pair(KeyName=key_pair_name)
        key_pair_path = os.path.join(KEY_PAIR_PATH, key_pair_name)
        with open(key_pair_path, 'wb') as fp:
            fp.write(key_pair.key_material.encode('ascii'))
        os.chmod(fp.name, 0o600)
    return key_pair


def dump_build_state(state):
    with open(BUILD_STATE_PATH, 'wb') as fp:
        pickle.dump(state, fp)


def dumps_biuld_state(func):
    @functools.wraps(func)
    def cmd_proxy(ctx, *args, **kwargs):
        ctx.obj['build-state'] = load_build_state()
        state = func(ctx, *args, **kwargs)
        dump_build_state(state)
    return cmd_proxy


def load_build_state():
    try:
        with open(BUILD_STATE_PATH, 'rb') as fp:
            state = pickle.load(fp)
    except IOError:
        state = {}
    return state


def _report_status(instance):
    echo_info('Instance Id: '
              '{0.instance_id}'.format(instance))
    echo_info('Instance Type: '
              '{0.instance_type}'.format(instance))
    echo_info('Instance Public DNS name: '
              '{0.public_dns_name}'.format(instance))
    echo_info('Instance Public IP Address: '
              '{0.public_ip_address}'.format(instance))
    echo_info('Tags: {}'.format(instance.tags))
    echo_info('Launched at: ' +
              instance.launch_time.isoformat(' '))


def _ensure_group(session, iam, group_name, group_policies):
    """Group must have the IAMReadOnlyAccess policy attached."""
    group = iam.Group(group_name)
    try:
        group.load()
    except ClientError:
        echo_error(
            'AWS IAM Group {!r} does not exist.'.format(group_name))
        # XXX: Use click's exit() method
        sys.exit(1)

    users = list(group.users.all())
    if not users:
        raise Exception('No users added to the "{}" '
                        'AWS IAM group {}'.format(group_name))
    group_policy_names = set(group_policies)
    for policy in iam.policies.all():
        if policy.policy_name in group_policy_names:
            group.attach_policy(policy.arn)
    return group


def _ensure_role(session, iam, assume_role_name, role_policies, group):
    role_map = {role.name: role for role in iam.roles.all()}
    role = role_map.get(assume_role_name)

    user_arns = list(user.arn for user in group.users.all())
    arp_doc = _make_asssume_role_policy(Principal=dict(AWS=user_arns))
    if role is None:
        role = iam.create_role(
            RoleName=assume_role_name,
            AssumeRolePolicyDocument=json.dumps(arp_doc))
    else:
        arp = role.AssumeRolePolicy()
        arp.update(arp_doc)
    role_policy_names = set(role_policies)
    for policy in iam.policies.all():
        if policy.policy_name in role_policy_names:
            role.attach_policy(PolicyArn=policy.arn)
    return role


def _ensure_assume_role_policy(session, iam, role, policy_name):
    pol_map = {pol.policy_name: pol
               for pol in iam.policies.filter(Scope='Local').all()}
    pol = pol_map.get(policy_name)
    if pol is None:
        policy_doc = _make_asssume_role_policy(Resource=role.arn)
        iam.create_policy(
            PolicyName=policy_name,
            Path='/',
            PolicyDocument=json.dumps(policy_doc),
            Description=('Allows the IAM user to which this policy '
                         'is attached to assume '
                         'the {role.name} role.'.format(role=role)))
    return pol


def _ensure_set(config, section, opt, new_value):
    val = config.get(section, opt)
    if val != new_value:
        opts = config[section]
        opts[opt] = new_value
        return True
    return False


def _ensure_config(ctx, session, role):
    assume_role_profile_name = '{.name}-assumer'.format(role)
    p_session = session._session
    config_file = p_session.get_config_variable('config_file')
    config_path = os.path.expanduser(config_file)
    config = configobj.ConfigObj(config_path, raise_errors=True)
    section = 'profile ' + assume_role_profile_name
    if section not in set(config):
        config.setdefault(section, {})
    ensure_set = functools.partial(_ensure_set, config, section)
    changes = []
    for (prop, val) in [('region', session.region_name),
                        ('role_arn', role.arn),
                        ('source_profile', session.profile_name),
                        ('role_session_name', '{.name}-assumed'.format(role))]:
        changes.append(ensure_set(prop, val))
    if any(changes):
        config.write()
    try:
        session = _aws_session(ctx, assume_role_profile_name)
        session.resource('iam')
        profile_name = assume_role_profile_name
    except ClientError:
        del config[section]['source_profile']
        config.write()
        profile_name = None
    return (session.profile_name, profile_name)


@click.group()
@option('--profile',
        default='default',
        help='AWS profile')
@option('--assume-role',
        default=IAM_DB_BUILD_ROLE,
        help='AWS Role that will be assumed to execute the build')
@click.pass_context
def cloud_ctrl(ctx, profile, assume_role):
    ctx.obj['profile'] = profile
    session = _aws_session(ctx, profile_name=profile)
    iam = session.resource('iam')
    role = iam.Role(assume_role)
    role.load()
    (profile_name, ar_profile_name) = _ensure_config(ctx, session, role)
    if ar_profile_name is not None:
        profiles = session._session.full_config['profiles']
        ar_profile = profiles[ar_profile_name]
        try:
            session = _aws_session(ctx, profile_name)
        except ClientError:
            pass
        else:
            ctx.obj['assumed_role'] = ar_profile['role_arn']
    ctx.obj['session'] = session
    ctx.obj['build-state'] = load_build_state()


@cloud_ctrl.command(short_help='Configure pre-requisit IAM roles and policies')
@click.argument('assume_role_name')
@option('--assume_role_policies',
        default=IAM_DB_BUILD_POLICIES,
        help='Policies to be attached to the assume role')
@option('--group-name',
        default=IAM_DB_BUILD_GROUP,
        help='Default IAM group for build users')
@click.pass_context
def setup_iam(ctx, assume_role_name, assume_role_policies, group_name):
    session = ctx.obj['session']
    iam = session.resource('iam')
    assume_role_policy_name = assume_role_name + '-assume'
    try:
        group = _ensure_group(session, iam, group_name)
        role = _ensure_role(session,
                            iam,
                            assume_role_name,
                            assume_role_policies,
                            group)
        _ensure_assume_role_policy(session, iam, role, assume_role_policy_name)
    except Exception as e:
        echo_error(e)
        ctx.abort()
    else:
        click.secho('Good to go!', fg='green')


@cloud_ctrl.command(short_help='Lists users allowed to perform the build')
@option('--group-name',
        default=IAM_DB_BUILD_GROUP,
        help=('Name of the AWS IAM group '
              'that contains users permitted '
              'to perform the build.'))
@option('--assume-role-name',
        default=IAM_ASSUME_ROLE_NAME,
        help=('The name of the role that users of '
              'the builders group use in order to assume role'))
@click.pass_context
def list_users(ctx, group_name, assume_role_name):
    """Display the IAM accounts allowed to perform the build.

    (Require admin privileges)
    """
    session = ctx.obj['session']
    iam = session.resource('iam')
    group = iam.Group(group_name)
    group.load()
    data = {'users': []}
    for user in sorted(group.users.all(), key=operator.attrgetter('name')):
        data['users'].append(dict(name=user.name, arn=user.arn))
    click.echo(json.dumps(data, indent=True, sort_keys=True))


@cloud_ctrl.command(short_help='Start the build process')
@option('--wb-db-build-version',
        default='0.1',
        help='The version of *this* python package')
@option('--dry-run',
        type=bool,
        default=False,
        help='Test to see if this command would execute.')
@option('--ami',
        # latest Amazon AMI (us-east-1, EBS-backed, 64-bit)
        default=EC2_INSTANCE_DEFAULTS['ami'],
        help='Amazon AMI Identifier. ')
@option('--monitoring',
        type=bool,
        default=True,
        help='Whether or not the instance has monitoring enabled.')
@option('--instance-type',
        default=EC2_INSTANCE_DEFAULTS['instance_type'],
        help='AWS EC2 Instance Type ')
@click.argument('sdist_path', metavar='<sdist>')
@click.argument('release', metavar='<WSXXX_release>')
@click.pass_context
@dumps_biuld_state
def kick_off(ctx,
             sdist_path,
             release,
             wb_db_build_version,
             ami,
             monitoring,
             instance_type,
             dry_run):
    """Start the build."""
    state = {}
    session = ctx.obj['session']
    ec2 = session.resource('ec2')
    key_pair = get_key_pair(ec2, release)
    format_user_data = USER_DATA_TEMPLATE.format
    sdist_filename = os.path.basename(sdist_path)
    with open(sdist_path, 'rb') as fp:
        data = base64.b64encode(fp.read())
        sdist_content = data.decode('utf-8')
    user_data = format_user_data(sdist_filename=sdist_filename,
                                 sdist_content=sdist_content)
    instance_options = dict(
        ImageId=ami,
        InstanceType=instance_type,
        KeyName=key_pair.name,
        MinCount=1,
        MaxCount=1,
        UserData=base64.b64encode(user_data.encode('utf-8')),
        Monitoring=dict(Enabled=monitoring),
        DryRun=dry_run)
    aws_userid = _aws_userid(session)
    state['release'] = release
    state['instance-options'] = instance_options
    instances = ec2.create_instances(**instance_options)
    instance = next(iter(instances))
    instance.create_tags(Tags=[
        dict(Key='CreatedBy', Value=aws_userid)])
    state['instance'] = dict(id=instance.id,
                             KeyPairName=key_pair.name)
    echo_waiting('Waiting for instance to enter running state ... ')
    instance.wait_until_running()
    echo_sig('done')
    _wait_for_sshd(instance)
    bootstrap(instance, wb_db_build_version)
    _report_status(instance)

    # XXX: DEBUG
    msg = 'ssh -i {0.key_pair.name} -l ec2-user {0.public_dns_name}'
    echo_info(msg.format(instance))

    state['instance-state'] = dict(instance.state)
    return state


@cloud_ctrl.command(short_help='Destroy ephemeral build resources')
@click.pass_context
@dumps_biuld_state
def destroy(ctx):
    state = ctx.obj['build-state']
    session = ctx.obj['session']
    ec2 = session.resource('ec2')
    instance_id = state['instance']['id']
    instances = ec2.instances.filter(InstanceIds=[instance_id])
    instance = next(iter(instances))
    instance.terminate()
    state['instance-state'] = instance.state
    echo_info('Instance {.id!r} is {[Name]}'.format(instance, instance.state))
    return state


@cloud_ctrl.command(short_help='Describe the state of the build')
@click.pass_context
@dumps_biuld_state
def show_state(ctx):
    state = ctx.obj['build-state']
    if 'instance' not in state:
        echo_info('No instances have been started')
        return
    session = ctx.obj['session']
    ec2 = session.resource('ec2')
    instance = ec2.Instance(state['instance']['id'])
    try:
        instance.load()
        instance_state = dict(instance.state)
    except (ClientError, AttributeError):
        instance_state = dict(Name='terminated?', code='<unknown>')
    state['instance-state'] = instance_state
    echo_info(pprint.pformat(state))
    return state


cli = cloud_ctrl(obj={})
