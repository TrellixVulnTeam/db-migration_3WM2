# -*- coding: utf-8 -*-
import base64
import contextlib
import functools
import json
import operator
import os
import pprint
import re
import shelve
import socket
import sys
import time

from botocore.exceptions import ClientError
from botocore.exceptions import ProfileNotFound
from pkg_resources import resource_filename
from scp import SCPClient
import boto3
import click
import configobj

from . import ssh
from .logging import get_logger
from .logging import setup_logging
from .util import distribution_name
from .util import echo_error
from .util import echo_info
from .util import echo_retry
from .util import echo_sig
from .util import echo_waiting
from .util import local
from .util import log_level_option
from .util import option

BUILD_STATE_PATH = os.path.join(os.getcwd(), '.db-build.db')

IAM_ASSUME_ROLE_NAME = 'wb-build-db-assume'

IAM_ASSUME_POLICY_NAME = 'wb-build-db-assume'

IAM_DB_BUILD_GROUP = 'wb-db-builders'

IAM_DB_BUILD_ROLE = 'wb-build-db'

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
    'IAMReadOnlyAccess',
    'AmazonEC2RoleforSSM'
)

USER_DATA_PATH = resource_filename(
    __package__,
    'cloud-config/AWS-cloud-config-UserData.template')

EC2_INSTANCE_DEFAULTS = dict(
    ami='ami-0dc73e60',  # <-- image of default Amazon AMI (2016)
    instance_type='c3.4xlarge',
    monitoring=False,
    dry_run=False
)

EC2_INSTANCE_ROLE = 'development'

logger = get_logger(__name__)


def load_ec2_instance_from_state(ctx, state):
    session = ctx.obj['session']
    ec2 = session.resource('ec2')
    instance = ec2.Instance(state['id'])
    instance.load()
    return instance


def wait_for_sshd(ec2_instance, max_timeout=60 * 6):
    waited = 0
    wait_msg = 'Waiting for connectivity to instance {.id}... '
    wait_msg = wait_msg.format(ec2_instance)
    while True:
        echo_waiting(wait_msg)
        s = socket.socket()
        s.settimeout(20)
        try:
            s.connect((ec2_instance.public_dns_name, 22))
            echo_sig('connected')
            break
        except Exception:
            time.sleep(20)
            waited += 40
        if waited >= max_timeout:
            msg = 'Failed to connect via ssh to {.public_dns_name}'
            msg = msg.format(ec2_instance)
            raise socket.timeout(msg)
        else:
            echo_retry('not yet, retrying')
    # To be sure...
    time.sleep(1)


@contextlib.contextmanager
def latest_build_state(ctx):
    bstate = ctx.obj['build-state']
    bstate.sync()
    curr_bstate = bstate.get('current')
    if curr_bstate is None:
        echo_error('No current instance to terminate.')
        echo_info('Other instances may be running, use AWS console')
        ctx.abort()
    try:
        instance = load_ec2_instance_from_state(ctx, curr_bstate)
        instance_state = dict(instance.state)
    except (ClientError, AttributeError):
        instance_state = dict(Name='terminated?', code='<unknown>')
    curr_bstate['instance-state'] = instance_state
    yield (instance, curr_bstate)


def get_archive_filename():
    # XXX: Path to filename produced by: python setup.py sdist (for now)
    # XXX: Best to download from github release.
    pkg_fullname = distribution_name()
    archive_filename = pkg_fullname + '.tar.gz'
    return archive_filename


def bootstrap(ec2_instance, package_version):
    """Deploy this package to the AWS instance.

    This involves scp'ing the data due to the repo being private.
    If, in the future this repo is deemed ok to be public,
    then this bootstraping via scp can be eliminated and
    commands run in UserData to fetch and install the data
    directly from a github release.

    This also requires the system package 'python3-dev'.
    """
    finished_regex = re.compile(r'Cloud-init.*finished')
    archive_filename = get_archive_filename()
    path = os.path.join('dist', archive_filename)
    local('python setup.py sdist')

    # Wait for cloud-init/config process to finish
    while True:
        with ssh.connection(ec2_instance) as conn:
            out = ssh.exec_command(
                conn,
                'tail -n1 /var/log/cloud-init-output.log')
        last_line = out.rstrip()
        if finished_regex.match(last_line) is not None:
            break
        time.sleep(30)

    # Upload the tar file
    with ssh.connection(ec2_instance) as conn:
        with SCPClient(conn.get_transport()) as scp:
            scp.put(path, archive_filename)

    # Now the wormbase-db-build package dependencies are available
    # and installation can proceed
    pip_install = 'python3 -m pip install --user '
    wbdb_install_cmd = pip_install + archive_filename
    pip_install_cmds = [pip_install + ' --upgrade pip',
                        wbdb_install_cmd]
    with ssh.connection(ec2_instance) as conn:
        for cmd in pip_install_cmds:
            try:
                out = ssh.exec_command(conn, cmd)
            except Exception:
                logger.exception()
            else:
                logger.debug(out)


def make_asssume_role_policy(version='2012-10-17', **attrs):
    attrs.setdefault('Effect', 'Allow')
    attrs.setdefault('Action', 'sts:AssumeRole')
    pol_stmt = dict(attrs)
    return dict(Version=version, Statement=[pol_stmt])


def aws_userid(session):
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


def aws_session(ctx, profile_name):
    try:
        session = boto3.Session(profile_name=profile_name)
    except ProfileNotFound as pnf:
        logger.error(str(pnf))
        ctx.abort()
    return session


def report_status(instance):
    if instance.meta.data is None:
        logger.info('No instance status to report.')
        return
    instance_state = instance.state.get('Name')
    is_active = instance_state != 'terminated'
    status = instance_state if is_active else 'terminated'
    logger.info('Instance Id: ' '{}', instance.id)
    logger.info('Tags: {}', instance.tags)
    logger.info('Launched at: ' + instance.launch_time.isoformat(' '))
    logger.info('Status: {}', status)
    if is_active:
        logger.info('Instance Type: {}', instance.instance_type)
        logger.info('Instance Public DNS name: {}', instance.public_dns_name)
        logger.info('Instance Public IP Address: {}',
                    instance.public_ip_address)


def ensure_group(session, iam, group_name, group_policies):
    """Group must have the IAMReadOnlyAccess policy attached."""
    group = iam.Group(group_name)
    try:
        group.load()
    except ClientError:
        logger.error(
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


def ensure_role(session, iam, assume_role_name, role_policies, group):
    role_map = {role.name: role for role in iam.roles.all()}
    role = role_map.get(assume_role_name)

    user_arns = list(user.arn for user in group.users.all())
    arp_doc = make_asssume_role_policy(Principal=dict(AWS=user_arns))
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


def ensure_assume_role_policy(session, iam, role, policy_name):
    pol_map = {pol.policy_name: pol
               for pol in iam.policies.filter(Scope='Local').all()}
    pol = pol_map.get(policy_name)
    if pol is None:
        policy_doc = make_asssume_role_policy(Resource=role.arn)
        iam.create_policy(
            PolicyName=policy_name,
            Path='/',
            PolicyDocument=json.dumps(policy_doc),
            Description=('Allows the IAM user to which this policy '
                         'is attached to assume '
                         'the {role.name} role.'.format(role=role)))
    return pol


def ensure_set(config, section, opt, new_value):
    val = config.get(section, opt)
    if val != new_value:
        opts = config[section]
        opts[opt] = new_value
        return True
    return False


def ensure_config(ctx, session, role):
    assume_role_profile_name = '{.name}-assumer'.format(role)
    p_session = session._session
    config_file = p_session.get_config_variable('config_file')
    config_path = os.path.expanduser(config_file)
    config = configobj.ConfigObj(config_path, raise_errors=True)
    section = 'profile ' + assume_role_profile_name
    if section not in set(config):
        config.setdefault(section, {})
    ensure_set_val = functools.partial(ensure_set, config, section)
    changes = []
    for (prop, val) in [('region', session.region_name),
                        ('role_arn', role.arn),
                        ('source_profile', session.profile_name),
                        ('role_session_name', '{.name}-assumed'.format(role))]:
        changes.append(ensure_set_val(prop, val))
    if any(changes):
        config.write()
    try:
        session = aws_session(ctx, assume_role_profile_name)
        session.resource('iam')
        profile_name = assume_role_profile_name
    except ClientError:
        del config[section]['source_profile']
        config.write()
        profile_name = None
    return (session.profile_name, profile_name)


@click.group()
@log_level_option(default='INFO')
@option('--profile',
        default='default',
        help='AWS profile')
@option('--assume-role',
        default=IAM_DB_BUILD_ROLE,
        help='AWS Role that will be assumed to execute the build')
@click.pass_context
def tasks(ctx, log_level, profile, assume_role):
    setup_logging(log_level=log_level)
    ctx.obj['profile'] = profile
    session = aws_session(ctx, profile_name=profile)
    iam = session.resource('iam')
    role = iam.Role(assume_role)
    role.load()
    (profile_name, ar_profile_name) = ensure_config(ctx, session, role)
    if ar_profile_name is not None:
        profiles = session._session.full_config['profiles']
        ar_profile = profiles[ar_profile_name]
        try:
            session = aws_session(ctx, profile_name)
        except ClientError:
            pass
        else:
            ctx.obj['assumed_role'] = ar_profile['role_arn']
    ctx.obj['session'] = session
    ctx.obj['build-state'] = shelve.open(BUILD_STATE_PATH)


@tasks.command(short_help='Configure pre-requisit IAM roles and policies')
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
        group = ensure_group(session, iam, group_name)
        role = ensure_role(session,
                           iam,
                           assume_role_name,
                           assume_role_policies,
                           group)
        ensure_assume_role_policy(session, iam, role, assume_role_policy_name)
    except Exception as e:
        echo_error(e)
        ctx.abort()
    else:
        echo_sig('Good to go!')


@tasks.command(short_help='Lists users allowed to perform the build')
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


@tasks.command(short_help='Start the build process')
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
@option('--keypair-name',
        default='wb-db-build',
        help='Name of EC2 KeyPair.')
@click.argument('sdist_path', metavar='<sdist>')
@click.argument('ws_data_release', metavar='<WSXXX data release>')
@click.pass_context
def init(ctx,
         sdist_path,
         ws_data_release,
         wb_db_build_version,
         ami,
         monitoring,
         instance_type,
         keypair_name,
         dry_run):
    """Start the build."""
    session = ctx.obj['session']
    state = ctx.obj['build-state']
    ec2 = session.resource('ec2')
    key_pair = ssh.recycle_key_pair(ec2, keypair_name)
    with open(USER_DATA_PATH) as fp:
        user_data = fp.read()
    instance_options = dict(
        ImageId=ami,
        InstanceType=instance_type,
        KeyName=key_pair.name,
        MinCount=1,
        MaxCount=1,
        UserData=base64.b64encode(user_data.encode('utf-8')),
        Monitoring=dict(Enabled=monitoring),
        DryRun=dry_run)
    created_by = aws_userid(session)
    instances = ec2.create_instances(**instance_options)
    instance = next(iter(instances))
    instance.create_tags(Tags=[
        dict(Key='CreatedBy', Value=created_by),
        dict(Key='Name', Value='wb-db-build-experimental'),
        dict(Key='Role', Value=EC2_INSTANCE_ROLE)])
    state[instance.id] = dict(id=instance.id,
                              init_options=instance_options,
                              KeyPairName=key_pair.name,
                              public_dns_name=instance.public_dns_name,
                              public_ip_addr=instance.public_ip_address,
                              started_by=session.profile_name,
                              ws_data_release=ws_data_release)
    state['current'] = state[instance.id]
    echo_waiting('Waiting for instance to enter running state ... ')
    instance.wait_until_running()
    echo_sig('done')
    wait_for_sshd(instance)
    echo_waiting('Bootstrapping instance with wormbase.db')
    bootstrap(instance, wb_db_build_version)
    echo_sig('done')
    report_status(instance)

    # XXX: DEBUG
    msg = 'ssh -i {0.key_pair.name} -l ec2-user {0.public_dns_name}'
    logger.info(msg.format(instance))

    state['instance-state'] = dict(instance.state)
    return state


@tasks.command(short_help='Terminate ephemeral build resources')
@click.pass_context
def terminate(ctx):
    with latest_build_state(ctx) as (instance, state):
        try:
            instance.terminate()
        except ClientError as client_error:
            msg = ('Only {[started-by]} or an adminstrator '
                   'will be able to terminate the instance')
            msg = msg.format(state)
            click.secho(str(client_error), fg='red')
            echo_error(msg)
        finally:
            state['instance-state'] = instance.state
        msg = 'Instance {.id!r} is {[Name]}'
        echo_info(msg.format(instance, instance.state))


@tasks.command(short_help='Describe the state of the build')
@click.pass_context
def view_state(ctx):
    with latest_build_state(ctx) as (_, state):
        echo_info(pprint.pformat(state))


@tasks.command(short_help='Describes the status of the build instance.')
@click.pass_context
def status(ctx):
    with latest_build_state(ctx) as (instance, _):
        report_status(instance)


cli = tasks(obj={})
