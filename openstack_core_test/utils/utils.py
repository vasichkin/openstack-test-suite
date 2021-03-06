import commands
import os
import time
import re
import tempfile
from urlparse import urlparse
from datetime import datetime
import string
from collections import namedtuple
import pexpect
from nose.tools import assert_equals, assert_true, assert_false
from pprint import pformat
import conf
from lettuce import world
import collections
from IPy import IP
import yaml
from lettuce_bunch.special import get_current_bunch_dir

# Make Bash an object
# Initialize global mappings with empty dicts. They are reassigned with "translate" properties at the end of file

world.instances = {}
world.images = {}
world.volumes = {}
world.floating = {}
world.novarc = {}
world.users = {}
world.tenants = {}
world.roles = {}
world.services = {}


DEFAULT_TIMEOUT=timeout=int(60)
DEFAULT_POLL_INTERVAL=int(5)


DEFAULT_FIXTURE = [
      ('role', 'add', 'Admin'),
      ('role', 'add', 'KeystoneServiceAdmin'),
      ('role', 'add', 'Member'),
      #  Services
      ('service', 'add', 'swift', 'object-store', 'Swift-compatible service'),
      ('service', 'add', 'nova',  'compute', 'OpenStack Compute Service'),
      ('service', 'add', 'nova_billing', 'nova_billing', 'Billing for OpenStack'),
      ('service', 'add', 'glance', 'image', 'OpenStack Image Service'),
      ('service', 'add', 'identity', 'identity', 'OpenStack Identity Service'),
]

DEFAULT_FIXTURE_PYTHON_KEYSTONE_CLIENT = [
      ('role-create', 'Admin'),
      ('role-create', 'KeystoneAdmin'),
      ('role-create', 'KeystoneServiceAdmin'),
      ('role-create', 'SomeRole'),

      ('service-create', '--name=nova', '--type=compute', '--description="Nova Compute Service"'),
      ('service-create', '--name=ec2',  '--type=ec2',     '--description="EC2 Compatibility Layer"'),
      ('service-create', '--name=glance', '--type=image', '--description="Glance Image Service"'),
      ('service-create', '--name=keystone', '--type=identity', '--description="Keystone Identity Service"'),
      ('service-create', '--name=swift', '--type=object-store', '--description="Swift Service"'),
]

ENDPOINT_TEMPLATES = {
      "swift": ('http://%host%:8080/v1', 'http://%host%:8080/v1', 'http://%host%:8080/v1', '1', '0'),
      "nova": ('http://%host%:8774/v1.1/%tenant_id%', 'http://%host%:8774/v1.1/%tenant_id%', 'http://%host%:8774/v1.1/%tenant_id%', '1', '0'),
      "glance": ('http://%host%:9292/v1', 'http://%host%:9292/v1', 'http://%host%:9292/v1', '1', '0'),
      "nova_billing": ('http://%host%:8787', 'http://%host%:8787', 'http://%host%:8787', '1', '1'),
      "identity": ('http://%host%:5000/v2.0', 'http://%host%:35357/v2.0', 'http://%host%:5000/v2.0', '1', '1'),
}


OUTPUT_GARBAGE = ['DeprecationWarning', 'import md5', 'import sha']

def wait(timeout=DEFAULT_TIMEOUT, poll_interval=DEFAULT_POLL_INTERVAL):
    def decorate(fcn):
        def f_retry(*args, **kwargs):
            time_left = int(timeout)
            while time_left > 0:
                if fcn(*args, **kwargs): # make attempt
                    return True
                time.sleep(poll_interval)
                time_left -= int(poll_interval)
            return False
        return f_retry
    return decorate

class command_output(object):
    def __init__(self, output):
        self.output = output

    def successful(self):
        return self.output[0] == 0

    def output_contains_pattern(self, pattern):
        regex2match = re.compile(pattern)
        search_result = regex2match.search(self.output[1])
        return (not search_result is None) and len(search_result.group()) > 0

    def output_text(self):
        def does_not_contain_garbage(str_item):
            for item in OUTPUT_GARBAGE:
                if item in str_item:
                    return False
            return True
        lines_without_warning = filter(does_not_contain_garbage, self.output[1].split(os.linesep))
        return string.join(lines_without_warning, os.linesep)


    def output_nonempty(self):
        return len(self.output) > 1 and len(self.output[1]) > 0

class bash(command_output):
    last_error_code = 0

    @classmethod
    def get_last_error_code(cls):
        return cls.last_error_code

    def __init__(self, cmdline):
        output = self.__execute(cmdline)
        super(bash,self).__init__(output)
        bash.last_error_code = self.output[0]

    def __execute(self, cmd):
        retcode = commands.getstatusoutput(cmd)
        status, text = retcode
        conf.bash_log(cmd, status, text)

#        print "------------------------------------------------------------"
#        print "cmd: %s" % cmd
#        print "sta: %s" % status
#        print "out: %s" % text

        return retcode



class rpm(object):

    @staticmethod
    def clean_all_cached_data():
        out = bash("sudo yum -q clean all")
        return out.successful()

    @staticmethod
    def install(package_list):
        out = bash("sudo yum -y install %s" % " ".join(package_list))
        return out.successful()

    @staticmethod
    def installed(package_list):
        out = bash("rpmquery %s" % " ".join(package_list))
        return out.successful() and not out.output_contains_pattern('not installed')

    @staticmethod
    def available(package_list):
        out = bash("sudo yum list %s" % " ".join(package_list))
        if not out.successful():
            return False
        lines = out.output_text().split("\n")
        for package in package_list:
            found = False
            for line in lines:
                if line.startswith("%s." % package):
                    found = True
                    break
            if not found:
                return False

        return True
        
    @staticmethod
    def remove(package_list):
#quotenize package namea
        packages=' '.join(map(lambda x: "'"+x+"'", " ".join(package_list).split()))
        out = bash("sudo yum -y erase %s" % packages)
#        wildcards_stripped_pkg_name = package.strip('*')
#        wildcards_stripped_pkg_name = " ".join(package_list)
#        return out.output_contains_pattern("(No Match for argument)|(Removed:[\s\S]*%s.*)|(Package.*%s.*not installed)" % (wildcards_stripped_pkg_name , wildcards_stripped_pkg_name))
        return out.successful()

    @staticmethod
    def yum_repo_exists(id):
        out = bash("sudo yum repolist | grep -E '^%s'" % id)
        return out.successful() and out.output_contains_pattern("%s" % id)


class EnvironmentRepoWriter(object):
    def __init__(self, repo, env_name=None):

        if env_name is None or env_name == 'master':
            repo_config = """
[{repo_id}]
name=Grid Dynamics OpenStack RHEL
baseurl=http://osc-build.vm.griddynamics.net/{repo_id}
enabled=1
gpgcheck=1

""".format(repo_id=repo)
        else:
            repo_config = """
[{repo_id}]
name=Grid Dynamics OpenStack RHEL
baseurl=http://osc-build.vm.griddynamics.net/unstable/{env}/{repo_id}
enabled=1
gpgcheck=1

""".format(repo_id=repo, env=env_name)
            pass

        self.__config = repo_config


    def write(self, file):
        file.write(self.__config)


class EscalatePermissions(object):

    @staticmethod
    def read(filename, reader):
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            tmp_file_path = tmp_file.name
        out = bash("sudo dd if=%s of=%s" % (filename, tmp_file_path))

        with open(tmp_file_path, 'r') as tmp_file:
            reader.read(tmp_file)
        bash("rm -f %s" % tmp_file_path)
        return out.successful()

    @staticmethod
    def overwrite(filename, writer):
        with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
            writer.write(tmp_file)
            tmp_file_path = tmp_file.name
        out = bash("sudo dd if=%s of=%s" % (tmp_file_path, filename))
        bash("rm -f %s" % tmp_file_path)
        return out.successful() and os.path.exists(filename)


class mysql_cli(object):
    @staticmethod
    def create_db(db_name, admin_name="root", admin_pwd="root"):
        bash("mysqladmin -u%s -p%s -f drop %s" % (admin_name, admin_pwd, db_name))
        out = bash("mysqladmin -u%s -p%s create %s" % (admin_name, admin_pwd, db_name))
        return out.successful()

    @staticmethod
    def execute(sql_command, admin_name="root", admin_pwd="root"):
        out = bash('echo "%s" | mysql -u%s -p%s mysql' % (sql_command, admin_name, admin_pwd))
        return out

    @staticmethod
    def update_root_pwd( default_pwd="", admin_pwd="root"):
        out = bash('mysqladmin -u root password %s' %  admin_pwd)
        return out.successful()

    @staticmethod
    def grant_db_access_for_hosts(hostname,db_name, db_user, db_pwd, admin_name="root", admin_pwd="root"):
        sql_command =  "GRANT ALL PRIVILEGES ON %s.* TO '%s'@'%s' IDENTIFIED BY '%s';" % (db_name, db_user, hostname, db_pwd)
        return mysql_cli.execute(sql_command, admin_name, admin_pwd).successful()

    @staticmethod
    def grant_db_access_local(db_name, db_user, db_pwd, admin_name="root", admin_pwd="root"):
        sql_command =  "GRANT ALL PRIVILEGES ON %s.* TO %s IDENTIFIED BY '%s';" % (db_name, db_user, db_pwd)
        return mysql_cli.execute(sql_command, admin_name, admin_pwd).successful()

    @staticmethod
    def db_exists(db_name, admin_name="root", admin_pwd="root"):
        sql_command = "show databases;"
        out = mysql_cli.execute(sql_command, admin_name, admin_pwd)
        return out.successful() and out.output_contains_pattern("%s" % db_name)

    @staticmethod
    def user_has_all_privileges_on_db(username, db_name, admin_name="root", admin_pwd="root"):
        sql_command = "show grants for '%s'@'localhost';" %username
        out = mysql_cli.execute(sql_command, admin_name, admin_pwd)
        return out.successful() \
            and out.output_contains_pattern("GRANT ALL PRIVILEGES ON .*%s.* TO .*%s.*" % (db_name, username))

    @staticmethod
    def user_has_all_privileges_on_db(username, db_name, admin_name="root", admin_pwd="root"):
        sql_command = "show grants for '%s'@'localhost';" %username
        out = mysql_cli.execute(sql_command, admin_name, admin_pwd)
        return out.successful() \
            and out.output_contains_pattern("GRANT ALL PRIVILEGES ON .*%s.* TO .*%s.*" % (db_name, username))

class service(object):
    def __init__(self, name):
        self.__name = name
        self.__unusual_running_patterns = {'rabbitmq-server': '(Node.*running)|(running_applications)'}
        self.__unusual_stopped_patterns = {'rabbitmq-server': 'no.nodes.running|no_nodes_running|nodedown|unrecognized'}
        self.__exec_by_expect = set(['rabbitmq-server'])

    def __exec_cmd(self, cmd):
        if self.__name in self.__exec_by_expect:
            return expect_run(cmd)

        return bash(cmd)

    def start(self):
        return self.__exec_cmd("sudo service %s start" % self.__name)

    def stop(self):
        return self.__exec_cmd("sudo service %s stop" % self.__name)

    def restart(self):
        return self.__exec_cmd("sudo service %s restart" % self.__name)

    def running(self):
        out = self.__exec_cmd("sudo service %s status" % self.__name)

        if self.__name in self.__unusual_running_patterns:
            return out.output_contains_pattern(self.__unusual_running_patterns[self.__name])

        return out.successful() \
            and out.output_contains_pattern("(?i)running") \
            and (not out.output_contains_pattern("(?i)stopped|unrecognized|dead|nodedown|waiting"))

    def stopped(self):
#        out = bash("sudo service %s status" % self.__name)
#        unusual_service_patterns = {'rabbitmq-server': 'no.nodes.running|no_nodes_running|nodedown'}
        out = self.__exec_cmd("sudo service %s status" % self.__name)

        if self.__name in self.__unusual_stopped_patterns:
            return out.output_contains_pattern(self.__unusual_stopped_patterns[self.__name])

        return (not out.output_contains_pattern("(?i)running")) \
            and out.output_contains_pattern("(?i)stopped|unrecognized|dead|nodedown|waiting")


class FlagFile(object):
    COMMENT_CHAR = '#'
    OPTION_CHAR =  '='

    def __init__(self, filename):
        self.__commented_options = set()
        self.options = {}
        self.__load(filename)

    def read(self, file):
        for line in file:
            comment = ''
            if FlagFile.COMMENT_CHAR in line:
                line, comment = line.split(FlagFile.COMMENT_CHAR, 1)
            if FlagFile.OPTION_CHAR in line:
                option, value = line.split(FlagFile.OPTION_CHAR, 1)
                option = option.strip()
                value = value.strip()
                if comment:
                    self.__commented_options.add(option)
                self.options[option] = value


    def __load(self, filename):
        EscalatePermissions.read(filename, self)

    def commented(self, option):
        return option in self.__commented_options

    def uncomment(self, option):
        if option in self.options and option in self.__commented_options:
            self.__commented_options.remove(option)

    def comment_out(self, option):
        if option in self.options:
            self.__commented_options.add(option)

    def write(self,file):
        for option, value in self.options.iteritems():
            comment_sign = FlagFile.COMMENT_CHAR if option in self.__commented_options else ''
            file.write("%s%s=%s\n" % (comment_sign, option, value))

    def remove_flags(self, flags):
        for name in flags:
            try:
                del self.options[name]
            except:
                pass
        return self

    def apply_flags(self, pairs):
        for name, value in pairs:
            self.options[name.strip()] = value.strip()
        return self

    def verify(self, pairs):
        for name, value in pairs:
            name = name.strip()
            value = value.strip()
            if name not in self.options or self.options[name] != value:
                return False
        return True

    def verify_existance(self, flags):
        for name in flags:
            name = name.strip()
            if name not in self.options:
                return False
        return True

    def overwrite(self, filename):
        return EscalatePermissions.overwrite(filename, self)

class novarc(dict):
    @staticmethod
    def load(file):
        novarc.file = file
        return os.path.exists(file)

    @staticmethod
    def source():
        return ". %s" % novarc.file

    @staticmethod
    def bash(cmd):
        return bash('. %s && %s' % (novarc.file, cmd))

#    @staticmethod
#    def create(project, user, password, destination, region):
#        if novarc.load(os.path.join(destination, 'novarc')):
#            return novarc
#        return None

    @staticmethod
    def generate(env, destination):
        novarc_path = os.path.join(destination, "novarc")
        novarc_text = ''
        for name in env.iteritems():
            world.novarc[name[0].strip()] = name[1].strip()

        for name in world.novarc.iteritems():
            novarc_text += 'export {0}={1}\n'.format(name[0],name[1])

        open(novarc_path, "wt").write(novarc_text)

        if novarc.load(os.path.join(destination, 'novarc')):
            return True
        return False

    @staticmethod
    def forget(destination):
        for name in world.novarc.iteritems():
            bash("unset %s" % name[0])
        world.novarc = {}
        return bash("mv -f %s %s" % (os.path.join(destination, "novarc"),os.path.join(destination, "novarc.old"))).successful

    @staticmethod
    def available(destination):
        if novarc.load(os.path.join(destination, 'novarc')):
            return True
        return False









        ##===================##
        ##  KEYSTONE MANAGE  ##
        ##===================##

class keystone_manage(object):
    @staticmethod
    def bash(cmd):
        return bash('sudo keystone-manage %s' % cmd).successful()

    @staticmethod
    def bash_check_out(cmd, pattern=''):
        out = bash('sudo keystone-manage %s' % cmd)
        return out.successful() and out.output_contains_pattern(".*%s.*" % pattern)

    @staticmethod
    def bash_out(cmd):
        out = bash('sudo keystone-manage %s' % cmd)
        garbage_list = ['DeprecationWarning', 'import md5', 'import sha']

        def does_not_contain_garbage(str_item):
            for item in garbage_list:
                if item in str_item:
                    return False
            return True

        lines_without_warning = filter(does_not_contain_garbage, out.output_text().split(os.linesep))
        return string.join(lines_without_warning, os.linesep)

    @staticmethod
    def init_default(host='127.0.0.1', user='admin', password='secrete',tenant='systenant', token='111222333444', region='regionOne'):
        for cmd in DEFAULT_FIXTURE:
            keystone_manage.bash("%s" % ' '.join(cmd))

        keystone_manage.create_tenant(tenant)

        i=int(1)
        for service in ENDPOINT_TEMPLATES:
            keystone_manage.add_template(
                region, service,
                *[word.replace("%host%", host)
                  for word in ENDPOINT_TEMPLATES[service]])

# ___ TODO ___
# FIX IT
            keystone_manage.add_endpoint(tenant,i)
            i=i+1
        return True

    @staticmethod
    def create_tenant(name):
        return keystone_manage.bash("tenant add %s" % name)


    @staticmethod
    def check_tenant_exist(name):
        return keystone_manage.bash_check_out("tenant list" , name)

    @staticmethod
    def delete_tenant(name):
        return keystone_manage.bash("tenant delete %s" % name)

    @staticmethod
    def create_user(name,password,tenant=''):
        return keystone_manage.bash("user add %s %s %s" % (name,password,tenant))

    @staticmethod
    def check_user_exist(name):
        return keystone_manage.bash_check_out("user list" , name)

    @staticmethod
    def delete_user(name):
        return keystone_manage.bash("user delete %s" % name)

    @staticmethod
    def delete_all_users():
        out = keystone_manage.bash_out("user list")
        for name in out.splitlines():
            keystone_manage.bash("user delete %s" % name)
        return True

    @staticmethod
    def add_role(name):
        return keystone_manage.bash("role add %s" % name)

    @staticmethod
    def check_role_exist(name):
        return keystone_manage.bash_check_out("role list" , name)

    @staticmethod
    def delete_role(name):
        return keystone_manage.bash("role delete %s" % name)

    @staticmethod
    def grant_role(role, user, tenant=None):
        if tenant in (None,''):
            return keystone_manage.bash("role grant %s %s" % (role,user))
        else:
            return keystone_manage.bash("role grant %s %s %s" % (role,user, tenant))
        return False

#__ TODO __
    @staticmethod
    def check_role_granted(role, user, tenant=None):
        #out = bash("sudo keystone-manage role grant %s %s" % (role,user))
        return True

    @staticmethod
    def revoke_role(role, user):
        keystone_manage.bash("role revoke %s %s" % (role,user))

    @staticmethod
    def add_template(region='', service='', publicURL='', adminURL='', internalURL='', enabled='1', isglobal='1'):
        return keystone_manage.bash("endpointTemplates add %s %s %s %s %s %s %s" % (region, service, publicURL, adminURL, internalURL, enabled, isglobal))

    @staticmethod
    def delete_template(region='', service='', publicURL='', adminURL='', internalURL='', enabled='1', isglobal='1'):
        return keystone_manage.bash("endpointTemplates delete %s %s %s %s %s %s %s" % (region, service, publicURL, adminURL, internalURL, enabled, isglobal))

    @staticmethod
    def add_token(user, tenant, token='111222333444', expiration='2015-02-05T00:00'):
        return keystone_manage.bash("token add %s %s %s %s" % (token, user, tenant, expiration))

#__ TODO __ (convert id to names, check it)
    @staticmethod
    def check_token_exist(user, tenant, token='111222333444', expiration='2015-02-05T00:00'):
        return keystone_manage.bash_check_out("token list", token)

    @staticmethod
    def delete_token(user, tenant, token='111222333444', expiration='2015-02-05T00:00'):
        return keystone_manage.bash("token delete %s" % token)

    @staticmethod
    def add_endpoint(tenant, template):
        return keystone_manage.bash("endpoint add %s %s" % (tenant, template))

    @staticmethod
    def delete_endpoint(tenant, template):
        return keystone_manage.bash("endpoint delete %s %s" % (tenant, template))

    @staticmethod
    def add_credential(user, tenant, service, key, secrete):
        return keystone_manage.bash("credentials add %s %s %s %s %s" % (user, service, key, secrete, tenant))

    @staticmethod
    def delete_credential(user, tenant, service, key, secrete):
        return keystone_manage.bash("credentials remove %s %s %s %s %s" % (user, service, key, secrete, tenant))

    @staticmethod
    def db_sync():
        return keystone_manage.bash("db_sync")






        ##===================##
        ##  KEYSTONE         ##
        ##===================##

class keystone(object):
    @staticmethod
    def bash(cmd):
        return novarc.bash('keystone %s' % cmd).successful()

    @staticmethod
    def bash_check_out(cmd, pattern=''):
        out = novarc.bash('keystone %s' % cmd)
        return out.successful() and out.output_contains_pattern(".*%s.*" % pattern)

    @staticmethod
    def bash_out(cmd):
        out = novarc.bash('keystone %s' % cmd)
        garbage_list = ['DeprecationWarning', 'import md5', 'import sha']

        def does_not_contain_garbage(str_item):
            for item in garbage_list:
                if item in str_item:
                    return False
            return True

        lines_without_warning = filter(does_not_contain_garbage, out.output_text().split(os.linesep))
        return string.join(lines_without_warning, os.linesep)

    @staticmethod
    def ec2_get_env():
        out=keystone.bash_out("ec2-credentials-create")
        if out:
            table = ascii_table(out)
            world.novarc["EC2_ACCESS_KEY"] = table.select_values('Value', 'Property', 'access')[0]
            world.novarc["EC2_SECRET_KEY"] = table.select_values('Value', 'Property', 'secret')[0]
            return True
        return False

    @staticmethod
    def tenant_create(name):
        out=keystone.bash_out("tenant-create --name=%s" % name)
        if out:
            table = ascii_table(out)
            gotten_id=table.select_values('Value', 'Property', 'id')
            if gotten_id:
                world.tenants[name]=gotten_id[0]
                return True
        return False

    @staticmethod
    def tenant_delete(name):
        return keystone.bash("tenant-delete %s" % world.tenants[name])

    @staticmethod
    def tenant_check(name, exist=True):
        if exist:
            return keystone.bash_check_out("tenant-list", world.tenants[name])
        else:
            return not keystone.bash_check_out("tenant-list", world.tenants[name])

    @staticmethod
    def role_create(name):
        out=keystone.bash_out("role-create --name=%s" % name)
        if out:
            table = ascii_table(out)
            gotten_id=table.select_values('Value', 'Property', 'id')
            if gotten_id:
                world.roles[name]=gotten_id[0]
                return True
        return False

    @staticmethod
    def role_delete(name):
        return keystone.bash("role-delete %s" % world.roles[name])

    @staticmethod
    def role_check(name, exist=True):
        if exist:
            return keystone.bash_check_out("role-list", world.roles[name])
        else:
            return not keystone.bash_check_out("role-list", world.roles[name])

    @staticmethod
    def role_add_user(role, user, tenant=None):
        return keystone.bash("user-role-add --user=%s --role=%s --tenant_id=%s" % (world.users[user], world.roles[role], world.tenants[tenant]))

    @staticmethod
    def role_remove_user(role, user, tenant=None):
        return keystone.bash("user-role-remove --user=%s --role=%s --tenant_id=%s" % (world.users[user], world.roles[role], world.tenants[tenant]))


    @staticmethod
    def user_create(name, password, tenant=None):
        if tenant:
            out=keystone.bash_out("user-create --name=%s --pass=%s --tenant_id" % (name, password, world.tenantss[tenant]))
        else:
            out=keystone.bash_out("user-create --name=%s --pass=%s" % (name,password))
        if out:
            table = ascii_table(out)
            gotten_id=table.select_values('Value', 'Property', 'id')
            if gotten_id:
                world.users[name]=gotten_id[0]
                return True
        return False

    @staticmethod
    def user_delete(name):
        return keystone.bash("user-delete %s" % world.users[name])

    @staticmethod
    def user_check(name, exist=True):
        if exist:
            return keystone.bash_check_out("user-list", world.users[name])
        else:
            return not keystone.bash_check_out("user-list", world.users[name])

    @staticmethod
    def service_create(name, stype, description="No description"):
        out=keystone.bash_out("service-create --name=%s --type=%s --description='%s'" % (name, stype, description))
        if out:
            table = ascii_table(out)
            gotten_id=table.select_values('Value', 'Property', 'id')
            if gotten_id:
                world.services[name]=gotten_id[0]
                return True
        return False

    @staticmethod
    def service_delete(name):
        return keystone.bash("service-delete %s" % world.services[name])

    @staticmethod
    def service_check(name, exist=True):
        if exist:
            return keystone.bash_check_out("service-list", world.services[name])
        else:
            return not keystone.bash_check_out("service-list", world.services[name])
            
            
            
            
            
            
            
            
            
            




    @staticmethod
    def check_tenant_exist(name):
        return keystone_manage.bash_check_out("tenant list" , name)

    @staticmethod
    def delete_tenant(name):
        return keystone_manage.bash("tenant delete %s" % name)

    @staticmethod
    def create_user(name,password,tenant=''):
        return keystone_manage.bash("user add %s %s %s" % (name,password,tenant))

    @staticmethod
    def check_user_exist(name):
        return keystone_manage.bash_check_out("user list" , name)

    @staticmethod
    def delete_user(name):
        return keystone_manage.bash("user delete %s" % name)

    @staticmethod
    def delete_all_users():
        out = keystone_manage.bash_out("user list")
        for name in out.splitlines():
            keystone_manage.bash("user delete %s" % name)
        return True

    @staticmethod
    def add_role(name):
        return keystone_manage.bash("role add %s" % name)

    @staticmethod
    def check_role_exist(name):
        return keystone_manage.bash_check_out("role list" , name)

    @staticmethod
    def delete_role(name):
        return keystone_manage.bash("role delete %s" % name)

    @staticmethod
    def grant_role(role, user, tenant=None):
        if tenant in (None,''):
            return keystone_manage.bash("role grant %s %s" % (role,user))
        else:
            return keystone_manage.bash("role grant %s %s %s" % (role,user, tenant))
        return False

#__ TODO __
    @staticmethod
    def check_role_granted(role, user, tenant=None):
        #out = bash("sudo keystone-manage role grant %s %s" % (role,user))
        return True

    @staticmethod
    def revoke_role(role, user):
        keystone_manage.bash("role revoke %s %s" % (role,user))

    @staticmethod
    def add_template(region='', service='', publicURL='', adminURL='', internalURL='', enabled='1', isglobal='1'):
        return keystone_manage.bash("endpointTemplates add %s %s %s %s %s %s %s" % (region, service, publicURL, adminURL, internalURL, enabled, isglobal))

    @staticmethod
    def delete_template(region='', service='', publicURL='', adminURL='', internalURL='', enabled='1', isglobal='1'):
        return keystone_manage.bash("endpointTemplates delete %s %s %s %s %s %s %s" % (region, service, publicURL, adminURL, internalURL, enabled, isglobal))

    @staticmethod
    def add_token(user, tenant, token='111222333444', expiration='2015-02-05T00:00'):
        return keystone_manage.bash("token add %s %s %s %s" % (token, user, tenant, expiration))

#__ TODO __ (convert id to names, check it)
    @staticmethod
    def check_token_exist(user, tenant, token='111222333444', expiration='2015-02-05T00:00'):
        return keystone_manage.bash_check_out("token list", token)

    @staticmethod
    def delete_token(user, tenant, token='111222333444', expiration='2015-02-05T00:00'):
        return keystone_manage.bash("token delete %s" % token)

    @staticmethod
    def add_endpoint(tenant, template):
        return keystone_manage.bash("endpoint add %s %s" % (tenant, template))

    @staticmethod
    def delete_endpoint(tenant, template):
        return keystone_manage.bash("endpoint delete %s %s" % (tenant, template))

    @staticmethod
    def add_credential(user, tenant, service, key, secrete):
        return keystone_manage.bash("credentials add %s %s %s %s %s" % (user, service, key, secrete, tenant))

    def delete_credential(user, tenant, service, key, secrete):
        return keystone_manage.bash("credentials remove %s %s %s %s %s" % (user, service, key, secrete, tenant))









        ##===============##
        ##  NOVA MANAGE  ##
        ##===============##

class nova_manage(object):
    @staticmethod
    def bash(cmd):
        return bash('sudo nova-manage %s' % cmd).successful()

    @staticmethod
    def bash_check_out(cmd, pattern=''):
        out = bash('sudo nova-manage %s' % cmd)
        return out.successful() and out.output_contains_pattern(".*%s.*" % pattern)

    @staticmethod
    def bash_out(cmd):
        out = bash('sudo nova-manage %s' % cmd)
        garbage_list = ['DeprecationWarning', 'import md5', 'import sha']

        def does_not_contain_garbage(str_item):
            for item in garbage_list:
                if item in str_item:
                    return False
            return True

        lines_without_warning = filter(does_not_contain_garbage, out.output_text().split(os.linesep))
        return string.join(lines_without_warning, os.linesep)


    @staticmethod
    def db_sync():
        return nova_manage.bash("db sync")

    @staticmethod
    def get_zipfile(project, user, destination):
        path = os.path.join(destination, 'novarc.zip')
        out = bash('sudo nova-manage project zipfile %s %s %s' % (project, user, path))
        if out.successful():
            bash("unzip -uo %s -d %s" % (path,destination+"/novarc_zip"))
            bash("cp -f %s/*.pem %s" % (destination+"/novarc_zip", destination))
        return out.successful()

    @staticmethod
    def export_ec2_keys(project, user, destination):
        out = nova_manage.bash_out('user exports --name %s' % user)
        for line in out.split('\n'):
            if 'export' in line:
                line=line.split(' ')[1]
                (parameter, value) = line.split('=')
                world.novarc[parameter]=value
        return nova_manage.get_zipfile(project, user, destination)

    @staticmethod
    def create_admin(username):
        return nova_manage.bash("user admin %s" % username)

    @staticmethod
    def delete_admin(username):
        return nova_manage.bash("user delete %s" % username)

    @staticmethod
    def delete_all_users():
        out = nova_manage.bash_out("user list")
        for name in out.splitlines():
            nova_manage.bash("user delete %s" % name)
        return True

    @staticmethod
    def user_exists(username):
        return nova_manage.bash_check_out("user list", username)

    @staticmethod
    def create_project(project_name, username):
        return nova_manage.bash("project create %s %s" % (project_name, username))

    @staticmethod
    def remove_project(project_name):
        return nova_manage.bash("project delete %s" % project_name)

    @staticmethod
    def delete_all_projects():
        out = nova_manage.bash_out("project list")
        for name in out.splitlines():
            nova_manage.bash("project delete %s" % name)
        return True

    @staticmethod
    def project_exists(project):
        return nova_manage.bash_check_out("project list", project)

    @staticmethod
    def user_is_project_admin(user, project):
        return nova_manage.bash_check_out("project list --user=%s" % user, project)

    @staticmethod
    def create_network(cidr, nets, ips):
        return nova_manage.bash('network create private "%s" %s %s' % (cidr, nets, ips))

    @staticmethod
    def create_network_new(cidr, label='mynet', bridge='eth0', vlan='100' ):
        return nova_manage.bash('network create --fixed_range_v4 %s --label "%s" --bridge_interface=%s --vlan=%s' % (cidr, label, bridge, vlan))

    @staticmethod
    def delete_network(cidr):
        return nova_manage.bash('network delete "%s"' % cidr)

    @staticmethod
    def delete_all_networks():
        out = nova_manage.bash_out("network list")
        for name in out.splitlines()[1:]:
            nova_manage.bash("network delete %s" % name.split()[1])
        return True

    @staticmethod
    def create_network_via_flags(flags_dict):
        params = ""
        for flag, value in flags_dict.items():
            params += " {flag}='{value}'".format(flag=flag, value=value)
        return nova_manage.bash('network create %s' % params)

    @staticmethod
    def network_exists(cidr):
        return nova_manage.bash_check_out('network list', cidr)

    @staticmethod
    def floating_add_pool(cidr):
        return nova_manage.bash('floating create %s' % cidr)

    @staticmethod
    def floating_remove_all_pools():
        if nova_manage.bash("floating list"):
            out = nova_manage.bash_out("floating list")
            for name in out.splitlines():
                nova_manage.bash("floating delete %s" % name.split()[1])
        return True


    @staticmethod
    def floating_remove_pool(cidr):
        return nova_manage.bash('floating delete %s' % cidr)

    @staticmethod
    def floating_check_pool(cidr):
        out = nova_manage.bash_out('floating list')
        ips=IP(cidr)

        for addr in ips:
            ip=IP(addr).strNormal()
            for line in out.split('\n'):
                if ip in line.split()[1]:
                    return True
        return False

    @staticmethod
    def vm_image_register(image_name, owner, disk, ram, kernel):
        if (ram and kernel) not in ('', None):
            out = nova_manage.bash('image all_register --image="%s" --kernel="%s" --ram="%s" --owner="%s" --name="%s"'
                % (disk, kernel, ram, owner, image_name))
        else:
            out = nova_manage.bash('image image_register --path="%s" --owner="%s" --name="%s"'
                % (disk, owner, image_name))

        return out



        ##============##
        ##  NOVA CLI  ##
        ##============##


class nova_cli(object):
    @staticmethod
    def exec_novaclient_cmd(cmd):
        return novarc.bash('nova %s' % cmd).successful()

    @staticmethod
    def get_novaclient_command_out(cmd):
        out = novarc.bash('nova %s' % cmd)
        garbage_list = ['DeprecationWarning', 'import md5', 'import sha']

        def does_not_contain_garbage(str_item):
            for item in garbage_list:
                if item in str_item:
                    return False
            return True

        lines_without_warning = filter(does_not_contain_garbage, out.output_text().split(os.linesep))
        return string.join(lines_without_warning, os.linesep)

    @staticmethod
    def ec2_get_keys(destination):
        return nova_cli.exec_novaclient_cmd("x509-get-root-cert %s/cacert.pem" % destination) and nova_cli.exec_novaclient_cmd("x509-create-cert %s/pk.pem %s/cert.pem" % (destination,destination))


    @staticmethod
    def vm_image_register(image_name, os_name):
#        ids=nova_cli.get_image_id_list(os_name)
#        if ids:
#            world.images[image_name]=ids[0]
#            return True
#        else:
#            return False

        try:
            world.images[image_name]=nova_cli.get_novaclient_command_out("image-list | grep -w ' %s ' | awk '{print $2}'" % os_name)
            return True
        except:
            return False




    @staticmethod
    def vm_image_registered(name):
        text = nova_cli.get_novaclient_command_out('image-list')
        if text:
            table = ascii_table(text)
            return table.select_values('ID','Name', name) is not None
        return False

    @staticmethod
    def get_image_status(name):
        text = nova_cli.get_novaclient_command_out('image-list')
        if text:
            table = ascii_table(text)
            return table.select_values('Status','Name', name)
        return False

    @staticmethod
    def add_keypair(name, public_key=None):
        public_key_param = "" if public_key is None else "--pub_key %s" % public_key
        return nova_cli.exec_novaclient_cmd('keypair-add %s %s' % (public_key_param, name))

    @staticmethod
    def delete_keypair(name):
        return nova_cli.exec_novaclient_cmd('keypair-delete %s' % name)


    @staticmethod
    def delete_all_keypairs():
        if nova_cli.exec_novaclient_cmd("keypair-list"):
            text = nova_cli.get_novaclient_command_out("keypair-list")
            table = ascii_table(text)
            for name in table.select_values('Name', 'Fingerprint', '*'):
                nova_cli.exec_novaclient_cmd('keypair-delete %s' % name)
        return True


    @staticmethod
    def keypair_exists(name):
        text = nova_cli.get_novaclient_command_out('keypair-list')
        if text:
            table = ascii_table(text)
            if table.select_values('Fingerprint','Name', name):
                return True
        return False


    @staticmethod
    def get_image_id_list(name):
        text = nova_cli.get_novaclient_command_out('image-list')
        if text:
            table = ascii_table(text)
            return table.select_values('ID','Name', name)
        return False


    @staticmethod
    def start_vm_instance1(name, image_name, flavor_name, key_name=None, secgroup=None):
        key_name_arg = "" if key_name is None else "--key_name %s" % key_name
        sgroup_arg = "" if secgroup is None else "--security_groups %s" % secgroup
        text = nova_cli.get_novaclient_command_out("boot %s --image %s --flavor %s %s %s" % (name, world.images[image_name], flavor_name, key_name_arg, sgroup_arg))
        if text and bash.get_last_error_code() == 0:
            table = ascii_table(text)
            instance_id = table.select_values('Value', 'Property', 'id')
            if instance_id:
                world.instances[name] = instance_id[0]
                return True
        return False

    @staticmethod
    def start_vm_instance(name, image_id, flavor_id, key_name=None, sec_groups=None):
        key_name_arg = "" if key_name is None else "--key_name %s" % key_name
        sgroup_arg = "" if sec_groups is None else "--security_groups %s" % sec_groups
        text = nova_cli.get_novaclient_command_out("boot %s --image %s --flavor %s %s %s" % (name, image_id, flavor_id, key_name_arg, sgroup_arg))
        if text and bash.get_last_error_code() == 0:
            table = ascii_table(text)
            instance_id = table.select_values('Value', 'Property', 'id')
            if instance_id:
                world.instances[name] = instance_id[0]
                return True
        return False

    @staticmethod
    def start_vm_instance_return_output(name, image_id, flavor_id, key_name=None):
        key_name_arg = "" if key_name is None else "--key_name %s" % key_name
        text =  nova_cli.get_novaclient_command_out("boot %s --image %s --flavor %s %s" % (name, image_id, flavor_id, key_name_arg))
        if text and bash.get_last_error_code() == 0:
            table = ascii_table(text)
            instance_id = table.select_values('Value', 'Property', 'id')
            if instance_id:
                world.instances[name] = instance_id[0]
            return ascii_table(text)
        return None


    @staticmethod
    def stop_vm_instance(name):
        return nova_cli.exec_novaclient_cmd("delete %s" % world.instances[name])

    @staticmethod
    def suspend_vm_instance(name):
        return nova_cli.exec_novaclient_cmd("suspend %s" % world.instances[name])

    @staticmethod
    def resume_vm_instance(name):
        return nova_cli.exec_novaclient_cmd("resume %s" % world.instances[name])

    @staticmethod
    def reboot_vm_instance(name):
        return nova_cli.exec_novaclient_cmd("reboot %s" % world.instances[name])


    @staticmethod
    def stop_all_vm_instances():
        text = nova_cli.get_novaclient_command_out("list")
        if text:
            table = ascii_table(text)
            for name in table.select_values('ID', 'Name', '*'):
                world.instances[name] = name
        for name in world.instances.keys():
            nova_cli.stop_vm_instance(name)
        return True

    @staticmethod
    def get_flavor_id_list(name):
        lines = nova_cli.get_novaclient_command_out("flavor-list | grep  %s | awk '{print $2}'" % name)
        id_list = lines.split(os.linesep)
        return id_list


    @staticmethod
    def get_instance_id_list(name):
        text = nova_cli.get_novaclient_command_out("list")
        if text and bash.get_last_error_code() == 0:
            table = ascii_table(text)
            ids = table.select_values('ID', 'Name',name)
            return ids
        return []


    @staticmethod
    def get_instance_status(name):
        text = nova_cli.get_novaclient_command_out("list")
        if text and bash.get_last_error_code() == 0:
            table = ascii_table(text)
            try:
                status = table.select_values('Status', 'ID',world.instances[name])[0]
                return status
            except:
                return None
        return None

    @staticmethod
    def get_instance_ip(name):
        text = nova_cli.get_novaclient_command_out("list")
        if text and bash.get_last_error_code() == 0:
            table = ascii_table(text)
            (status,ip) = table.select_values('Networks', 'ID',world.instances[name])[0].split('=')
            ip = ip.split(',')[0]
            return ip
        return False


    @staticmethod
    def wait_instance_state(instance_name, state, timeout, expect_fail=False):
        @wait(timeout=timeout)
        def polling_function(name):
            status = nova_cli.get_instance_status(name)
            if status:
                if status.upper() == state.upper():
                    result=True
                else:
                    result=False
                if result and not(expect_fail):
                    return True
                elif not(result) and expect_fail:
                    return True
                else:
                    return False
            elif expect_fail:
                return True

            print "--- No status bad here, false"
            return False
        return polling_function(instance_name)

    @staticmethod
    def wait_instance_comes_up(name, timeout):
        return nova_cli.wait_instance_state(name, 'ACTIVE', timeout)

    @staticmethod
    def wait_instance_stopped(name, timeout):
        return nova_cli.wait_instance_state(name, 'ACTIVE', timeout, expect_fail=True)


#        @wait(timeout=timeout)
#        def polling_function(name):
#            if not nova_cli.get_instance_status(name):
#                return True
#            return False
#        return polling_function(name)

    @staticmethod
    def billed_objects(project, min_instances, min_images):
        out = nova_cli.__novarc.bash("nova2ools-billing list --images --instances | grep '^Project %s$' -A 4" % project)
        if not out.successful():
            return False
        lines = out.output_text().split("\n")
        if len(lines) < 4:
            return False
        if not lines[1].startswith("instances:") or int(lines[1].split(" ")[1]) < min_instances:
            return False
        if not lines[3].startswith("images:") or int(lines[3].split(" ")[1]) < min_images:
            return False
        return True


    @staticmethod
    def floating_allocate(name):
        text = nova_cli.get_novaclient_command_out('floating-ip-create')
        if text:
            table = ascii_table(text)
            world.floating[name] = table.select_values('Ip','Instance', 'None')[0]
            if world.floating[name]:return True
        return False

    @staticmethod
    def floating_deallocate(name):
        return nova_cli.exec_novaclient_cmd('floating-ip-delete %s' % world.floating[name])

    @staticmethod
    def floating_deallocate_all():
        if nova_cli.exec_novaclient_cmd('floating-ip-list'):
            text = nova_cli.get_novaclient_command_out('floating-ip-list')
            table = ascii_table(text)
            for name in table.select_values('Ip', 'Pool', '*'):
                world.floating[name] = name
        if world.floating:
            for name in world.floating.keys():
                nova_cli.floating_deallocate(name)
        return True

    @staticmethod
    def floating_check_allocated(name):
        if not world.floating[name]: world.floating[name]=None
        text = nova_cli.get_novaclient_command_out('floating-ip-list')
        if text:
            table = ascii_table(text)
            try:
                value = table.select_values('Instance', 'Ip',  world.floating[name])[0]
                if value in ('None',):
                    return True
            except:
                return False
        return False


    @staticmethod
    def floating_associate(addr_name, ins_name):
        return nova_cli.exec_novaclient_cmd('add-floating-ip %s %s' % (world.instances[ins_name],world.floating[addr_name]))

    @staticmethod
    def floating_deassociate(addr_name, ins_name):
        return nova_cli.exec_novaclient_cmd('remove-floating-ip %s %s' % (world.instances[ins_name],world.floating[addr_name]))


    @staticmethod
    def floating_check_associated(addr_name, ins_name):
        if not world.floating[addr_name]: world.floating[addr_name]=None
        text = nova_cli.get_novaclient_command_out('floating-ip-list')

        if text:
            table = ascii_table(text)
            if table.select_values('Instance', 'Ip', world.floating[addr_name])[0]==world.instances[ins_name]:
                return True
        return False

    @staticmethod
    def get_floating_ip_list(name):
        text = nova_cli.get_novaclient_command_out("floating-ip-list")
        if text and bash.get_last_error_code() == 0:
            table = ascii_table(text)
            ips = table.select_values('Ip', 'Instance',name)
            return ips
        return []


    @staticmethod
    def get_volume_id_list(name):
        text = nova_cli.get_novaclient_command_out("volume-list")
        if text and bash.get_last_error_code() == 0:
            table = ascii_table(text)
            ids = table.select_values('ID', 'Display Name',name)
            return ids
        return []


    @staticmethod
    def volume_create(name,size,zone='nova'):
        if nova_cli.exec_novaclient_cmd("volume-create --display_name %s --display_description '%s' %s" % (name, "OSCT autocreated volume", size)):
            world.volumes[name] = nova_cli.get_volume_id_list(name)[0]
            return True
        return False


    @staticmethod
    def get_volume_status(volume_name):
        out = nova_cli.get_novaclient_command_out("volume-list |grep %s" % volume_name)
        k=1
        status={}
        values=out.split("|")
        if values:
            try:
                for key in ('ID', 'Status', 'Display Name', 'Size', 'Volume Type', 'Attached to'):
                    status[key.lower()]=values[k].strip()
                    k=k+1
            except:
                return False
        if status: return status
        else: return False

    @staticmethod
    @wait()
    def wait_volume_comes_up(volume_name, timeout):
        status = nova_cli.get_volume_status(volume_name)['status']
        if 'available' in status:
            return True
        return False

    @staticmethod
    @wait()
    def wait_volume_state(volume_name, state, timeout):
        status = nova_cli.get_volume_status(volume_name)['status']
        if state in status:
            return True
        return False

    @staticmethod
    def volume_attach(instance_name, dev, volume_name):
        return nova_cli.exec_novaclient_cmd("volume-attach  %s %s %s" % (world.instances[instance_name], world.volumes[volume_name], dev))

    @staticmethod
    @wait(120)
    def volume_attached_to_instance(volume_name, instance_name):
        status = nova_cli.get_volume_status(volume_name)
        if ('in-use' in status['status']) and (world.instances[instance_name] in status['attached to']):
            return True
        return False

    @staticmethod
    def volume_detach(volume_name, instance_name):
        return nova_cli.exec_novaclient_cmd("volume-detach  %s %s" % (world.instances[instance_name], world.volumes[volume_name]))

    @staticmethod
    @wait()
    def check_volume_deleted(volume_name):
        if not nova_cli.get_volume_status(volume_name):
            return True
        return False

    @staticmethod
    def volume_delete(volume_name):
        return nova_cli.exec_novaclient_cmd("volume-delete  %s" % world.volumes[volume_name])

    @staticmethod
    def volume_delete_all():
        try:
            text = nova_cli.get_novaclient_command_out("volume-list")
            table = ascii_table(text)
            for volume_id in table.select_values('ID', 'Status', '*'):
                nova_cli.exec_novaclient_cmd('volume-detach %s' % volume_id)
                nova_cli.exec_novaclient_cmd('volume-delete %s' % volume_id)
            return True
        except:
            return False



        ##============##
        ##  EUCA CLI  ##
        ##============##


class euca_cli(object):

    @staticmethod
    def _parse_rule(dst_group=None, source_group_user=None,source_group=None, proto=None, source_subnet=None, port=None):
        params={}
        if dst_group: dst_group=str(dst_group)
        if source_group_user: source_group_user=str(source_group_user)
        if source_group: source_group=str(source_group)

        if port:
            try:
                if not port=='-1--1':
                    from_port, to_port = port.split('-')
            except:
                port=port+"-"+port


        if proto:
            proto=str(proto)
            if proto.upper() in ('ICMP',):
                if port in ('-1', '-1:-1','-1--1', '', None):
                    params['protocol']="icmp"
                    params['icmp-type-code']="-1:-1"
                else:
                    params['protocol']='icmp'
                    params['icmp-type-code']=port
            if proto.upper() in ('TCP', 'UDP'):
                params['protocol']=proto
                params['port-range']=port

        if source_subnet in ('', None, '0', 'any'):
            params['source-subnet']='0.0.0.0/0'
        else:
            params['source-subnet']=source_subnet

        if source_group:
            params['source-group']=source_group

        if source_group_user:
            params['source-group-user']=source_group_user

        cmdline=[]
        for param,val in sorted(params.iteritems()):
            cmdline.append(' --'+param+' '+val)

        if dst_group:
            cmdline.append(' '+dst_group)
        else:
            cmdline.append(' default')

#        print "\nPARSE-PARAMS: %s"  % cmdline
        return ''.join(cmdline)


    @staticmethod
    def volume_create(name,size,zone='nova'):
        out = novarc.bash("euca-create-volume --size %s --zone %s" % (size, zone))
        euca_id=None
        if out:
            for line in out.output_text().split('\n'):
                if 'VOLUME' in line:
                    if not euca_id:
                        euca_id = line.split()[1]
                        world.volumes[name] = misc.get_nova_id(euca_id)
                    else:
                        return False
        return out.successful()

    @staticmethod
    def get_volume_status(volume_name):
        volume_id='vol-'+misc.get_euca_id(nova_id=world.volumes[volume_name])
        out = novarc.bash("euca-describe-volumes |grep %s" % volume_id).output_text()

        badchars=['(', ')', ',']
        for char in badchars:
            out=out.replace(char, ' ')
        k=1
        status={}
        values=out.split()
        if values:
            for key in ('volume_id', 'size', 'zone', 'status', 'project', 'host', 'instance', 'device', 'date'):
                status[key]=values[k]
                k=k+1
            if status['instance'] != 'None':
                ins = status['instance']
                ins = ins.replace(']','')
                status['instance'], status['instance-host'] = ins.split('[')
        if status: return status
        else: return False


    @staticmethod
    @wait()
    def wait_volume_comes_up(volume_name, timeout):
        status = euca_cli.get_volume_status(volume_name)['status']
        if 'available' in status:
            return True
        return False

    @staticmethod
    def volume_attach(instance_name, dev, volume_name):
        volume_id='vol-'+misc.get_euca_id(nova_id=world.volumes[volume_name])
        instance_id='i-'+misc.get_euca_id(nova_id=world.instances[instance_name])
        out = novarc.bash('euca-attach-volume --instance %s --device %s %s' % (instance_id, dev, volume_id))
        out = novarc.bash('euca-attach-volume --instance %s --device %s %s' % (instance_id, dev, volume_id))
        return out.successful()

    @staticmethod
    @wait(120)
    def volume_attached_to_instance(volume_name, instance_name):
        volume_id='vol-'+misc.get_euca_id(nova_id=world.volumes[volume_name])
        instance_id='i-'+misc.get_euca_id(nova_id=world.instances[instance_name])
        status = euca_cli.get_volume_status(volume_name)
        if ('in-use' in status['status']) and (instance_id in status['instance']):
            return True
        return False

    @staticmethod
    def volume_detach(volume_name):
        volume_id='vol-'+misc.get_euca_id(nova_id=world.volumes[volume_name])
        out = novarc.bash('euca-detach-volume %s' % volume_id)
        return out.successful()

    @staticmethod
    @wait()
    def check_volume_deleted(volume_name):
        if not euca_cli.get_volume_status(volume_name):
            return True
        return False

    @staticmethod
    def volume_delete(volume_name):
        volume_id='vol-'+misc.get_euca_id(nova_id=world.volumes[volume_name])
        out = novarc.bash("euca-delete-volume %s" % volume_id)
        return out.successful()


    @staticmethod
    def volume_delete_all():
        out = novarc.bash("euca-describe-volumes").output_text()
        for name in out.splitlines():
            if 'vol-' in name:
                world.volumes[name.split()[1]] = misc.get_nova_id(name.split()[1])
        if world.volumes:
            for volume_name in world.volumes.keys():
                euca_cli.volume_detach(volume_name)
                euca_cli.volume_delete(volume_name)
                euca_cli.check_volume_deleted(volume_name)
        return True

    @staticmethod
    def sgroup_add(group_name):
        return novarc.bash('euca-add-group -d integration-tests-secgroup-test %s' % group_name).successful()

    @staticmethod
    def sgroup_delete(group_name):
        return novarc.bash('euca-delete-group %s' % group_name).successful()

    @staticmethod
    def sgroup_delete_all():
        out = novarc.bash("euca-describe-groups | grep GROUP").output_text()
        for name in out.splitlines():
            novarc.bash("euca-delete-group %s" % name.split()[2])
        return True

    @staticmethod
    def sgroup_check(group_name):
        out = novarc.bash("euca-describe-groups %s |grep GROUP |awk '{print $3}'" % group_name).output_text()
        if group_name in out:
            return True
        return False

    @staticmethod
    def sgroup_add_rule(dst_group='', src_group='', src_proto='', src_host='', dst_port=''):
        params = euca_cli._parse_rule(dst_group, '', src_group, src_proto, src_host, dst_port)
        return novarc.bash('euca-authorize %s' % params).successful()

    @staticmethod
    def sgroup_del_rule(dst_group='', src_group='', src_proto='', src_host='', dst_port=''):
        params = euca_cli._parse_rule(dst_group, '', src_group, src_proto, src_host, dst_port)
        return novarc.bash('euca-revoke %s' % params).successful()


    @staticmethod
    def sgroup_check_rule_exist(dst_group='', src_group='', src_proto='', src_host='', dst_port=''):
        out=novarc.bash('euca-describe-groups %s|grep PERMISSION' % dst_group).output_text()
        rule = euca_cli._parse_rule(dst_group, '', src_group, src_proto, src_host, dst_port)
#        print "Searching: "+rule

        # Try to assign to vars values as in euca-authorize output
        if out:
            for line in out.split('\n'):
#                print "Got line: "+line
                if 'FROM' in line:
                    (gperm, gproj, ggroup, grule, gproto, gport_from, gport_to, gfr, gci, ghost)=line.split()
#                    print "FR-OUT-line: "+euca_cli._parse_rule(ggroup, '', '', gproto, ghost, gport_from+"-"+gport_to)
                    if rule == euca_cli._parse_rule(ggroup, '', '', gproto, ghost, gport_from+"-"+gport_to):
                        return True

                elif 'GRPNAME' in line:
                    try:
                        (gperm, gproj, ggroup, grule, gproto, gport_from, gport_to, ggr, gsrc_group)=line.split()
#                        print "GR-OUT-line: "+euca_cli._parse_rule(ggroup, '', gsrc_group, gproto, '', gport_from+"-"+gport_to)
                        if rule == euca_cli._parse_rule(ggroup, '', gsrc_group, gproto, '', gport_from+"-"+gport_to):
                            return True
                    except:
                        return False
        return False

    @staticmethod
    def sgroup_check_rule(dst_group='', src_group='', src_proto='', src_host='', dst_port=''):
        # Workaround for group rule
        #PERMISSION      project1        smoketest3      ALLOWS  icmp    -1      -1      USER    project1
        #PERMISSION      project1        smoketest3      ALLOWS  tcp     1       65535   USER    project1
        #PERMISSION      project1        smoketest3      ALLOWS  udp     1       65536   USER    project1

        if src_group and (src_proto=='' and src_host=='' and dst_port==''):
            if euca_cli.sgroup_check_rule_exist(dst_group, src_group, src_proto='tcp', src_host='', dst_port='1-65535') and\
            euca_cli.sgroup_check_rule_exist(dst_group, src_group, src_proto='udp', src_host='', dst_port='1-65536') and \
            euca_cli.sgroup_check_rule_exist(dst_group, src_group, src_proto='icmp', src_host='', dst_port='-1'):
                return True
        return euca_cli.sgroup_check_rule_exist(dst_group, src_group, src_proto, src_host, dst_port)

    @staticmethod
    def vm_image_register(image_name, owner, disk, ram, kernel):
        pathdir = bash('mktemp -d').output_text().strip()

        if (ram and kernel) not in ('', None):
            novarc.bash('euca-bundle-image --image %s --destination %s -p kernel --kernel true' % (kernel, pathdir))
            novarc.bash('euca-bundle-image --image %s --destination %s -p ramdisk --ramdisk true' % (ram, pathdir))

            novarc.bash('euca-upload-bundle -m %s/kernel.manifest.xml -b %s' % (pathdir, image_name))
            novarc.bash('euca-upload-bundle -m %s/ramdisk.manifest.xml -b %s' % (pathdir, image_name))

            ami_kernel=novarc.bash('euca-register %s/kernel.manifest.xml' % image_name).split()[-1]
            ami_ramdisk=novarc.bash('euca-register %s/ramdisk.manifest.xml' % image_name).split()[-1]

            novarc.bash('euca-bundle-image -p machine -i  %s --kernel %s --ramdisk %s ' % (disk,ami_kernel,ami_ramdisk))
            novarc.bash('euca-upload-bundle -m %s/machine.manifest.xml -b %s' %      (pathdir, image_name))

            ami_machine=Common.bash('euca-register %s/machine.manifest.xml' % image_name).split()[-1]


        else:
            novarc.bash('euca-bundle-image --image %s --destination %s -p %s' % (disk, pathdir, image_name))
            novarc.bash('euca-upload-bundle -m %s/%s.manifest.xml -b %s' % (pathdir, image_name, image_name))
            novarc.bash('euca-register %s/%s.manifest.xml' % (pathdir ,image_name))

        bash('rm -rf %s' % pathdir )
        return True



    @staticmethod
    def get_image_id_list(image_name):
        text = novarc.bash('euca-describe-images').output_text()
        image_ids = []
        if text:
            for line in text.splitlines():
                """IMAGE   ami-00000001    None (solid_mini_image)         available       public                  machine                 instance-store"""
                """IMAGE   ami-00000001    None (solid_mini_image)         available       public                x86_64  machine                 instance-store"""
                try:
                    (img, euca_image_id, descr, name, status, acl, imgtype, location) = line.split()
                except:
                    (img, euca_image_id, descr, name, status, acl, arch, imgtype, location) = line.split()
                if name.strip('() ') == image_name.strip():
                    image_ids.append(euca_image_id)
            return image_ids
        return False


    @staticmethod
    def vm_image_registered(image_name):
        try:
            image_id = euca_cli.get_image_id_list(image_name)[0]
            return True
        except:
            return False



        ##===================##
        ##  GLANCE           ##
        ##===================##


class glance_cli(object):

    @staticmethod
    def glance_add(image_file, format, **kwargs):
        out = novarc.bash(
            'glance add disk_format=%s container_format=%s is_public=True %s < "%s"'
            % (format,
               format,
               " ".join(["%s=%s" % (key, value)
                         for key, value in kwargs.iteritems()]),
               image_file))
        if not out.successful() or not "Added new image with ID:" in out.output_text():
            return None
        return int(out.output_text().split(':')[1])

    @staticmethod
    def vm_image_all_register(image_name, owner, disk, ram, kernel):
        kernel_id = glance_cli.glance_add(kernel, "aki", name="%s_kernel" % image_name)
        if kernel_id is None:
            return False
        ramdisk_id = glance_cli.glance_add(kernel, "ari", name="%s_ramdisk" % image_name)
        if ramdisk_id is None:
            return False
        rootfs_id = glance_cli.glance_add(
            kernel, "ami", name=image_name, kernel_id=kernel_id, ramdisk_id=ramdisk_id)
        return rootfs_id is not None

    @staticmethod
    def get_image_id_list(image_name):
        text = novarc.bash('glance index').output_text()
        if text:
            table = ascii_table(text, " ")
            return table.select_values('ID','Name', image_name)
        return False


    @staticmethod
    def vm_image_registered(image_name):
        try:
            image_id = glance_cli.get_image_id_list(image_name)[0]
            return image_id and novarc.bash('glance show %s' % image_id)
        except:
            return False


    @staticmethod
    def vm_image_register(image_name, owner, image_file):
        out = novarc.bash('glance add disk_format=raw is_public=True name=%s < "%s"'
            % (image_name, image_file))

        rootfs_id = int(out.output_text().split(':')[1])
        return rootfs_id is not None

    @staticmethod
    def deregister_all_images():
        return bash("sudo glance -f clear").successful()

    @staticmethod
    def deregister_image(image_name):
        try:
            image_id = nova_cli.get_image_id_list(image_name)[0]
            return novarc.bash("glance -f delete %s" % image_id).successful()
        except:
            return False




        ##===================##
        ##  NOVA2OOLS        ##
        ##===================##


class nova2ools_cli(object):
    #nova2ools-local-volumes
    @staticmethod
    def local_volume_create(instance_name, size=None, device=None, snapshot_name = None):
        params=' --vm %s' % world.instances[instance_name]
        if snapshot_name:
            try: 
                image_id = nova_cli.get_image_id_list(snapshot_name)[0]
                params += ' --snapshot %s' % image_id
            except:
                return False
        if size: params += ' --size %s' % size
        if device: params += ' --device %s' % device
        return novarc.bash("nova2ools-local-volumes create %s" % params).successful()


    @staticmethod
    def local_volume_delete(instance_name, device):
        return novarc.bash("nova2ools-local-volumes delete --id %s" % nova2ools_cli.get_local_volume_id(instance_name, device)).successful()

    @staticmethod
    def get_local_volume_state(instance_name, device):
        out = novarc.bash("nova2ools-local-volumes list --format %s" % '"{instance_id} {device} {status}"').output_text()
        for line in out.splitlines():
            instance_id, dev, status = line.split()
            if instance_id.strip() == world.instances[instance_name].strip():
                if dev.strip() == device.strip():
                    return status
        return False

    @staticmethod
    def get_local_volume_id(instance_name, device):
        out = novarc.bash("nova2ools-local-volumes list --format %s" % '"{instance_id} {device} {status} {id}"').output_text()
        for line in out.splitlines():
            instance_id, dev, status, vol_id = line.split()
            if instance_id.strip() == world.instances[instance_name].strip():
                if dev.strip() == device.strip():
                    return vol_id
        return False

    @staticmethod
    def get_local_volume_size(instance_name, device):
        out = novarc.bash("nova2ools-local-volumes list --format %s" % '"{instance_id} {device} {status} {id} {size}"').output_text()
        for line in out.splitlines():
            instance_id, dev, status, vol_id, size = line.split()
            if instance_id.strip() == world.instances[instance_name].strip():
                if dev.strip() == device.strip():
                    return size
        return False

    @staticmethod
    def wait_local_volume_state(instance_name, device, state, timeout=timeout, expect_fail=False):
        @wait(int(timeout))
        def polling_function(name):
            if nova2ools_cli.get_local_volume_state(name, device) and (nova2ools_cli.get_local_volume_state(name, device).upper() == state.upper()):
                return True
            elif (not nova2ools_cli.get_local_volume_state(name, device)) and expect_fail:
                return True
        return polling_function(instance_name)

    @staticmethod
    def local_volume_snapshot(instance_name, device, snapshot_name):
        return novarc.bash("nova2ools-local-volumes snapshot --id %s --name %s" % (nova2ools_cli.get_local_volume_id(instance_name, device), snapshot_name)).successful()

    @staticmethod
    def wait_local_volume_snapshot_state(snapshot_name, state, timeout=timeout, expect_fail=False):
        @wait(int(timeout))
        def polling_function(name):
            if nova_cli.get_image_status(name) and (nova_cli.get_image_status(name)[0].upper() == state.upper()):
                return True
            elif expect_fail:
                return True
        return polling_function(snapshot_name)

    @staticmethod
    def local_volume_resize(instance_name, device, size):
        return novarc.bash("nova2ools-local-volumes resize --id=%s --size=%s" % (nova2ools_cli.get_local_volume_id(instance_name, device),size)).successful()

    #nova2ools-vms
    @staticmethod
    def check_nova2ools_vms_list(instance_name, user=None, project=None, status=None, key_name=None):
        params = {'name': instance_name, 'user_id': user, 'tenant_name': project, 'status': status, 'key_name': key_name}

        parameters_string = " ".join(['{' + params[param] + '}' for param in params if params[param] != None])
        raw_dict = tuple((param, params[param]) for param in params if param != None)
        expected_params = dict(raw_dict)
        expected_string = parameters_string.format(**expected_params)

        res = novarc.bash("nova2ools-vms list -f \"%s\"" % (parameters_string))

        if res.successful():
            out = res.output_text().split('\n')

            for line in out:
                if line == expected_string:
                    return True
        return False

    #nova2ools-images
    @staticmethod
    def get_image_id_list(image_name):
        text = novarc.bash('nova2ools-images list').output_text()
        image_ids = []
        if text:
            for line in text.splitlines():
                (name, image_id, format, status) = line.split()
                if name.strip() == image_name.strip():
                    image_ids.append(image_id.split(',')[1])
            return image_ids
        return False


    @staticmethod
    def vm_image_registered(image_name):
        try:
            image_id = nova2ools_cli.get_image_id_list(image_name)[0]
            return True
        except:
            return False

## TODO  ## TODO  ## TODO  ## TODO  
    @staticmethod
    def vm_image_register(image_name, owner, disk, ram, kernel):
        if (ram and kernel) not in ('', None):
            out = novarc.bash('nova2ools-images register-all --image="%s" --kernel="%s" --ram="%s" --name="%s" --public'
                % (disk, kernel, ram, image_name))
        else:
            out = novarc.bash('nova2ools-images register --path="%s" --name="%s" --public'
                % (disk, image_name))
        return out.successful()




class misc(object):

    @staticmethod
    def kill_process(name):
        bash("sudo killall  %s" % name).successful()
        return True

    @staticmethod
    def unzip(zipfile, destination="."):
        out = bash("unzip %s -d %s" % (zipfile,destination))
        return out.successful()

    @staticmethod
    def extract_targz(file, destination="."):
        out = bash("tar xzf %s -C %s" % (file,destination))
        return out.successful()

    @staticmethod
    def remove_files_recursively_forced(wildcard):
        out = bash("sudo rm -rf %s" % wildcard)
        return out.successful()

    @staticmethod
    def no_files_exist(wildcard):
        out = bash("sudo ls -1 %s | wc -l" % wildcard)
        return out.successful() and out.output_contains_pattern("(\s)*0(\s)*")

    @staticmethod
    def install_build_env_repo(repo, env_name=None):
        return EscalatePermissions.overwrite('/etc/yum.repos.d/os-env.repo', EnvironmentRepoWriter(repo,env_name))

    @staticmethod
    def generate_ssh_keypair(file):
        bash("rm -f %s" % file)
        return bash("ssh-keygen -N '' -f {file} -t rsa -q".format(file=file)).successful()

    @staticmethod
    def can_execute_sudo_without_pwd():
        out = bash("sudo -l")
        return out.successful() and out.output_nonempty() \
            and (out.output_contains_pattern("\(ALL\)(\s)*NOPASSWD:(\s)*ALL")
                or out.output_contains_pattern("\(ALL : ALL\)(\s)*NOPASSWD:(\s)*ALL")
                or out.output_contains_pattern("User root may run the following commands on this host"))

    @staticmethod
    def create_loop_dev(loop_dev,loop_file,loop_size):
        return bash("test -e %s || dd if=/dev/zero of=%s bs=1024 count=%s" % (loop_file, loop_file, int(loop_size)*1024*1024)).successful() and bash("sudo losetup %s %s" % (loop_dev,loop_file)).successful()

    @staticmethod
    def delete_loop_dev(loop_dev,loop_file=""):
        if not loop_file:
            loop_file = bash("sudo losetup %s | sed 's/.*(\(.*\)).*/\1/'" % loop_dev).output_text()[0]
        return bash("sudo losetup -d %s" % loop_dev).successful() 
        # and bash("rm -f %s" % loop_file).successful()

    @staticmethod
    def check_loop_dev_exist(loop_dev):
        out = bash("sudo pvscan -s | grep %s" % loop_dev).output_text()
        if loop_dev in out:
            return True
        return False

    @staticmethod
    def create_lvm(lvm_dev,lvm_group="nova-volumes"):
        return bash("sudo pvcreate %s" % lvm_dev).successful() and bash("sudo vgcreate %s %s" % (lvm_group,lvm_dev)).successful()

    @staticmethod
    def delete_lvm(lvm_dev,lvm_group="nova-volumes"):
        return bash("sudo vgremove -f %s" % lvm_group).successful() and bash("sudo pvremove -y -ff %s" % lvm_dev).successful()

    @staticmethod
    def check_lvm_available(lvm_dev,lvm_group="nova-volumes"):
        out = bash("sudo vgscan | grep %s" % lvm_group).output_text()
        out1 = bash("sudo pvscan | grep %s" % lvm_group).output_text()
        if lvm_group in out:
            if (lvm_dev in out1) and (lvm_group in out1):
                return True
        return False

    @staticmethod
    def get_euca_id(nova_id=None, name=None):
        if nova_id:
            return '{0:008x}'.format(int(nova_id))
        elif name:
            return 'TODO'
        else: return False

    @staticmethod
    def get_nova_id(euca_id=None, name=None):
        if euca_id:
            return int(euca_id.split('-')[1],16)
        elif name:
            return 'TODO'
        else: return False


class ascii_table(object):
    def __init__(self, str, separator = '|'):
        self.titles, self.rows = self.__construct(str, separator)


    def __construct(self, str, separator):

        column_titles = None
        rows = []
        for line in str.splitlines():
            if separator in line:
                if separator in (None, ' ', ''):
                    dirty_row =  map(string.strip, line.split('  '))
                    row = [x for x in dirty_row if x != '']
                else:
                    row =  map(string.strip, line.strip(separator).split(separator))
                if column_titles is None:
                    column_titles = [rw.split()[0] for rw in row]
                else:
                    if not '---' in row[0]:
                        rows.append(row)
        Row = namedtuple('Row', column_titles)
        rows = map(Row._make, rows)
        return column_titles, rows

    def select_values(self, from_column, where_column, items_equal_to):
        from_column_number = self.titles.index(from_column.split()[0])
        where_column_name_number = self.titles.index(where_column.split()[0])
        if items_equal_to == '*':
            return [item[from_column_number] for item in self.rows]
        else:
            return [item[from_column_number] for item in self.rows if item[where_column_name_number] == items_equal_to]

class expect_spawn(pexpect.spawn):
    def get_output(self, code_override=None):
        text_output = "before:\n{before}\nafter:\n{after}\ninternals:\n{internals}".format(
            before = self.before if isinstance(self.before, basestring) else pformat(self.before, indent=4),
            after = self.after if isinstance(self.after, basestring) else pformat(self.after, indent=4),
            internals = str(self))

        if code_override is not None:
            conf.bash_log(pformat(self.args), code_override, text_output)
            return code_override, text_output

        if self.isalive():
            conf.bash_log(pformat(self.args), 'Spawned process running: pid={pid}'.format(pid=self.pid), text_output)
            raise pexpect.ExceptionPexpect('Unable to return exit code. Spawned command is still running:\n' + text_output)

        conf.bash_log(pformat(self.args), self.exitstatus, text_output)
        return self.exitstatus, text_output

class expect_run(command_output):
    def __init__(self, cmdline):
        output = self.__execute(cmdline)
        super(expect_run,self).__init__(output)

    def __execute(self, cmd):
        text, status = pexpect.run(cmd,withexitstatus=True)
        conf.bash_log(cmd, status, text)
        return status, text

class ssh(command_output):
    def __init__(self, host, command=None, user=None, key=None, password=None):

        options='-q -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
        user_prefix = '' if user is None else '%s@' % user

        if key is not None: options += ' -i %s' % key

        cmd = "ssh {options} {user_prefix}{host} {command}".format(options=options,
                                                                   user_prefix=user_prefix,
                                                                   host=host,
                                                                   command=command)

        conf.log(conf.get_bash_log_file(),cmd)

        if password is None:
            super(ssh,self).__init__(bash(cmd).output)
        else:
            super(ssh,self).__init__(self.__use_expect(cmd, password))

    def __use_expect(self, cmd, password):
        spawned = expect_spawn(cmd)
        triggered_index = spawned.expect([pexpect.TIMEOUT, pexpect.EOF, 'password:'])
        if triggered_index == 0:
            return spawned.get_output(-1)
        elif triggered_index == 1:
            return spawned.get_output(-1)

        spawned.sendline(password)
        triggered_index = spawned.expect([pexpect.EOF, pexpect.TIMEOUT])
        if triggered_index == 1:
            spawned.terminate(force=True)

        return spawned.get_output()


class networking(object):

    class http(object):
        @staticmethod
        def probe(url):
            return bash('curl --silent --head %s | grep "200 OK"' % url).successful()

        @staticmethod
        def get(url, destination="."):
            return bash('wget -nv --directory-prefix="%s" %s' % (destination, url)).successful()

        @staticmethod
        def basename(url):
            return os.path.basename(urlparse(url).path)

    class icmp(object):
        @staticmethod
        def probe(ip, timeout):
            @wait(timeout)
            def polling_function(ip):
                return bash("ping -c3 %s" % ip).successful()
            return polling_function(ip)

    class nmap(object):
        @staticmethod
        def open_port_serves_protocol(host, port, proto, timeout):
            @wait(timeout=timeout)
            def polling_function(host, port, proto):
                return bash('nmap -PN -p %s --open -sV %s | '\
                            'grep -iE "open.*%s"' % (port, host, proto)).successful()
            return polling_function(host,port, proto)

    class ifconfig(object):
        @staticmethod
        def interface_exists(name):
            return bash('sudo ifconfig %s' % name).successful()

        @staticmethod
        def set(interface, options):
            return bash('sudo ifconfig {interface} {options}'.format(interface=interface, options=options)).successful()



    class brctl(object):
        @staticmethod
        def create_bridge(name):
            return bash('sudo brctl addbr %s' % name).successful()

        @staticmethod
        def delete_bridge(name):
            return networking.ifconfig.set(name, 'down') and bash('sudo brctl delbr %s' % name).successful()

        @staticmethod
        def add_interface(bridge, interface):
            return bash('sudo brctl addif {bridge} {interface}'.format(bridge=bridge, interface=interface)).successful()

    class ip(object):
        class addr(object):
            @staticmethod
            def show(param_string):
                return bash('sudo ip addr show %s' % param_string)

#decorator for performing action on step failure
def onfailure(*triggers):
    def decorate(fcn):
        def wrap(*args, **kwargs):
            try:
                retval = fcn(*args, **kwargs)
            except:
                for trigger in triggers:
                    trigger()
                raise
            return retval
        return wrap

    return decorate


class debug(object):
    @staticmethod
    def current_bunch_path():
#        global __file__
#        return __file__
        return get_current_bunch_dir()

    class save(object):
        @staticmethod
        def file(src, dst):
            def saving_function():
                bash("sudo dd if={src} of={dst}".format(src=src,dst=dst))
            return saving_function

        @staticmethod
        def command_output(command, file_to_save):
            def command_output_function():
                dst = os.path.join(debug.current_bunch_path(),file_to_save)
                conf.log(dst, bash(command).output_text())
            return command_output_function

        @staticmethod
        def nova_conf():
            debug.save.file('/etc/nova/nova.conf', os.path.join(debug.current_bunch_path(), 'nova.conf.log'))()

        @staticmethod
        def log(logfile):
            src = logfile if os.path.isabs(logfile) else os.path.join('/var/log', logfile)
            dst = os.path.basename(src)
            dst = os.path.join(debug.current_bunch_path(), dst if os.path.splitext(dst)[1] == '.log' else dst + ".log")
            return debug.save.file(src, dst)


class MemorizedMapping(collections.MutableMapping,dict):
    class AmbiguousMapping(Exception):
        pass

    class EmptyResultForKey(Exception):
        pass

    def __init__(self, restore_function=None,store_function=None, **kwargs):
        self.__rst_fcn = restore_function
        self.__store_fcn = store_function
        super(MemorizedMapping, self).__init__(**kwargs)

    def __getitem__(self,key):
        if not self.__contains__(key) and self.__rst_fcn is not None:
            items = self.__rst_fcn(key)
            if len(items) > 1:
                raise MemorizedMapping.AmbiguousMapping(items)
            elif not len(items):
                raise MemorizedMapping.EmptyResultForKey(key)
            else:
                self[key] = items[0]

        return dict.__getitem__(self,key)

    def __setitem__(self, key, value):
        if self.__store_fcn is not None:
            self.__store_fcn(key, value)

        dict.__setitem__(self,key,value)

    def __delitem__(self, key):
        dict.__delitem__(self,key)

    def __iter__(self):
        return dict.__iter__(self)

    def __len__(self):
        return dict.__len__(self)

    def __contains__(self, x):
        return dict.__contains__(self,x)


class translate(object):
    @classmethod
    def register(cls, name, restore_function=None, store_function=None):
        """
        Register property 'name' which acts as mapping of keys and values:
        translate.name[key] -> value
        if key is not found for dictionary name name, then
        it is tried to be resolved by callng mapping_function(key).
        mapping_function(key) -> [value1, value2, ...]
        If value is not unique for key, then exception is raised. The same happens if list is empty
        """
        setattr(cls, name, MemorizedMapping(restore_function=restore_function, store_function=store_function))

    @classmethod
    def unregister(cls, name):
        delattr(cls, name)

class SerializeMapping(object):
    @staticmethod
    def mapping_file(mapping_name):
        return os.path.join(debug.current_bunch_path(), mapping_name + '.map')


    @staticmethod
    def restore_fcn(mapping_name):
        filename = SerializeMapping.mapping_file(mapping_name)
        def restore_fcn(key):
            if os.path.exists(filename):
                with open(filename, 'r') as map_file:
                    mapping = yaml.load(map_file)
                    return [mapping[key]]
            else:
                return []
        return restore_fcn

    @staticmethod
    def store_fcn(mapping_name):
        filename = SerializeMapping.mapping_file(mapping_name)
        def store_fcn(key, value):
            mapping = {}
            if os.path.exists(filename):
                with open(filename, 'r') as map_file:
                    mapping = yaml.load(map_file)
            mapping[key] = value
            with open(filename, 'w') as map_file:
                map_file.write(yaml.dump(mapping, default_flow_style=False))

        return store_fcn

#translate.register('instances', restore_function=nova_cli.get_instance_id_list)
translate.register('instances',
    restore_function=SerializeMapping.restore_fcn('instances'),
    store_function=SerializeMapping.store_fcn('instances'))

translate.register('images',
    restore_function=SerializeMapping.restore_fcn('images'),
    store_function=SerializeMapping.store_fcn('images'))
#translate.register('volumes', restore_function=nova_cli.get_volume_id_list)
translate.register('volumes',
    restore_function=SerializeMapping.restore_fcn('volumes'),
    store_function=SerializeMapping.store_fcn('volumes'))
translate.register('floating',
    restore_function=SerializeMapping.restore_fcn('floating'),
    store_function=SerializeMapping.store_fcn('floating'))

translate.register('users',
    restore_function=SerializeMapping.restore_fcn('users'),
    store_function=SerializeMapping.store_fcn('users'))

translate.register('tenants',
    restore_function=SerializeMapping.restore_fcn('tenants'),
    store_function=SerializeMapping.store_fcn('tenants'))

translate.register('roles',
    restore_function=SerializeMapping.restore_fcn('roles'),
    store_function=SerializeMapping.store_fcn('roles'))

translate.register('services',
    restore_function=SerializeMapping.restore_fcn('services'),
    store_function=SerializeMapping.store_fcn('services'))


world.instances = translate.instances
world.images = translate.images
world.volumes = translate.volumes
world.floating = translate.floating
world.users = translate.users
world.tenants = translate.tenants
world.roles = translate.roles
world.services = translate.services
