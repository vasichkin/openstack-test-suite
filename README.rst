openstack-test-suite
====================

This suite was created to test functionality of openstack.


Installation:

git clone https://github.com/vasichkin/openstack-test-suite.git
cd openstack-test-suite
python setup.py install



Configuration:
Configuration file located in scenario folder, - integration-tests/ubuntu-essex/config.yaml
In general to run tests you need to configure:
 - image.name - name of preloaded image.
 - server.external - ip address of cloud controller (if needed. 127.0.0.1 by default)
 - location of conf files of services (if needed)
 - net.cloud.cidr - network for instances (if needed)
All other options could be changed if needed. Purpose of each parameter could be guessed or seen in scenario scripts.

Prerequisites:

To run tests you need:
- Preconfigured openstack (essex release) with working keystone authorization;
- Services must be installed and ready to run without errors: nova- (api,compute,sceduler,cert,network,volume), glance, keystone;
- Image for instance spawning must be uploaded and changed in config.yaml
- Test should be run under user, able to run sudo wuthout password

Running test:
cd openstack-test-suite/integration-tests
bunch ubuntu-essex ./result_dir

results will be available in "./result_dir" dir
