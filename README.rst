openstack-test-suite
====================

This suite was created to test functionality of openstack.

Prerequisites
-------------

To run tests you need
- Preconfigured openstack (essex release) with working keystone authorization;
- Services must be installed and ready to run without errors: nova- (api,compute,sceduler,cert,network,volume), glance, keystone;
- Image for instance spawning must be uploaded and changed in config.yaml
- Test should be run under user, able to run sudo wuthout password or by root user.

What this suite does
--------------------

In a scenario scripts directory (integration-tests/ubuntu-essex/) you can see what steps are perfomed. It's human readble text.

In a short, basic test does (01-keystone-instance.test):
- Makes changes to configuration files to perform initial keystone configuration;
- Creates project, user, network, user keys, checking tha all goes fine;
- Spawns instance and check it is accessible from outside;
- Stops instance, removes project, user, network, user keys checking every step does what it should.

There are also tests to check security groups, floating ip, volumes functionality.
All scripts using BDD style, so it's human-readble text.


Installation
------------

Get code as tarball or via github. Unpack it.
    cd openstack-test-suite
    python setup.py install


Configuration
-------------

Configuration file located in scenario folder, - integration-tests/ubuntu-essex/config.yaml
In general to run tests you need to configure:
- image.name - name of preloaded image.
- server.external - ip address of cloud controller (if needed. 127.0.0.1 by default)
- location of conf files of services (if needed)
- net.cloud.cidr - network for instances (if needed)
All other options could be changed if needed. Purpose of each parameter could be guessed or seen in scenario scripts.


Running test
------------

  cd openstack-test-suite/integration-tests
  bunch ubuntu-essex ./result_dir

results will be available in "./result_dir" dir

After running tests, in a result dir, you can find:
 - reports for every scenario run
 - log file "bash.log"
 - keys and other authorization files

Troubleshooting
---------------
Progress of the tests is reported to ./result_der/bash.log in details, so you can always repeat actions performed to debug them.