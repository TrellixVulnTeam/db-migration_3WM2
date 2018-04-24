.. _db-migration-user-guide:

============================
Database Migration Procedure
============================
The database migration will be performed on an :term:`EC2` instance.
Upon successful completion, the migrated Datomic database will be
stored in :term:`S3` storage.

The migration process will take approximately 3½ days to complete.

`<Notifications :ref:`Notifications`> will be sent before and after each step of
the migration process.

*Any* person having a WormBase Amazon :term:`AWS` account will be
 capable of performing the migration procedure.

.. todolist::

.. toctree::
   :hidden:
   :maxdepth: 1

   prerequisites
   notifications
   setup
   commands


