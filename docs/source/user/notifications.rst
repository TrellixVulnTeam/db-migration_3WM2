.. notifications:

=============
Notifications
=============
The database migration processes will send notifications to the
`#wb-db-migration-events` channel (:term:`Slack` team: wormbase-db-dev).

Notifications are sent before and after each build step; at the end of
the process a notification will sent to confirm the migrated Datomic
databases’ location on Amazon :term:`S3` storage To configure
notifications for you and the WormBase team:

Enter the `slack webhook url`_ to the following command:

.. code-block:: bash

  azanium configure <SLACK_WEBHOOK_URL>


.. _`slack webhook url`: https://wormbase-db-dev.slack.com/services/B1HNK2JEM#service_setup
