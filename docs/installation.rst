Installing Vumi with VirtualBox and Vagrant
===========================================

The easiest way to try out Vumi is by using
VirtualBox_ and Vagrant_:

    1. Install VirtualBox_
    2. Install Vagrant_, make sure you follow the `OS specific instructions`_.
    3. Clone Vumi with ``git clone https://github.com/praekelt/vumi.git``
    4. Execute ``cd vumi``
    5. Execute ``vagrant up``, this will take some time to complete as it:
        1. Downloads a 480MB Ubuntu 10.04 server VM
        2. Downloads ~ 40MB worth of Python packages
        3. Downloads ~ 100MB worth of packages with `apt-get`
    6. Shell into the machine via ``vagrant ssh``, you'll find the Vumi installation in ``/var/praekelt/vumi``.
    7. Follow the instructions at :ref:`first-smpp-bind` to setup your first Vumi application.

.. note::

    Ubuntu 10.04 only provides version 1.3.5 of RubyGems while
    Vagrant requires version 1.3.6. You'll need to get an upstream
    version or install from source as per the `OS specific
    instructions`_.

.. note::

    Ubuntu doesn't put `vagrant` on your $PATH, you'll need to
    manually symlink it with `sudo ln -s /var/lib/gems/1.8/bin/vagrant
    /usr/bin/`

.. _Vagrant: http://www.vagrantup.com
.. _VirtualBox: http://www.virtualbox.org
.. _OS specific instructions: http://vagrantup.com/v1/docs/getting-started/index.html
