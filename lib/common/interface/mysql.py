import os
import time
import socket
import MySQLdb

from common.interface.inventory import InventoryInterface
from common.dataformat import Dataset, Block, Site, DatasetReplica, BlockReplica
import common.configuration as config

class MySQLInterface(InventoryInterface):
    """Interface to MySQL."""

    class DatabaseError(Exception):
        pass

    def __init__(self):
        super(MySQLInterface, self).__init__()

        self._db_params = {'host': config.mysql.host, 'user': config.mysql.user, 'passwd': config.mysql.passwd, 'db': config.mysql.db}
        self.connection = MySQLdb.connect(**self._db_params)

    def _do_acquire_lock(self): #override
        while True:
            # Use the system table to "software-lock" the database
            self._query('LOCK TABLES `system` WRITE')
            self._query('UPDATE `system` SET `lock_host` = %s, `lock_process` = %s WHERE `lock_host` LIKE \'\' AND `lock_process` = 0', socket.gethostname(), os.getpid())

            # Did the update go through?
            host, pid = self._query('SELECT `lock_host`, `lock_process` FROM `system`')[0]
            self._query('UNLOCK TABLES')

            if host == socket.gethostname() and pid == os.getpid():
                # The database is locked.
                break

            if config.debug_level > 0:
                print 'Failed to database. Waiting 30 seconds..'

            time.sleep(30)

    def _do_release_lock(self): #override
        self._query('LOCK TABLES `system` WRITE')
        self._query('UPDATE `system` SET `lock_host` = \'\', `lock_process` = 0 WHERE `lock_host` LIKE %s AND `lock_process` = %s', socket.gethostname(), os.getpid())

        # Did the update go through?
        host, pid = self._query('SELECT `lock_host`, `lock_process` FROM `system`')[0]
        self._query('UNLOCK TABLES')

        if host != '' or pid != 0:
            raise InventoryInterface.LockError('Failed to release lock from ' + socket.gethostname() + ':' + str(os.getpid()))

    def _do_make_snapshot(self, clear): #override
        db = self._db_params['db']
        new_db = self._db_params['db'] + time.strftime('_%y%m%d%H%M%S')

        self._query('CREATE DATABASE `%s`' % new_db)

        tables = self._query('SHOW TABLES')

        for table in tables:
            self._query('CREATE TABLE `%s`.`%s` LIKE `%s`.`%s`' % (new_db, table, db, table))
            if table != 'system':
                self._query('INSERT INTO `%s`.`%s` SELECT * FROM `%s`.`%s`' % (new_db, table, db, table))

                if clear:
                    self._query('DROP TABLE `%s`.`%s`' % (db, table))
                    self._query('CREATE TABLE `%s`.`%s` LIKE `%s`.`%s`' % (db, table, new_db, table))
       
        self._query('INSERT INTO `%s`.`system` (`lock_host`,`lock_process`) VALUES (\'\',0)' % new_db)

    def _do_load_data(self): #override
        site_list = {}
        dataset_list = {}

        sites = self._query('SELECT `id`, `name`, `host`, `storage_type`, `backend`, `capacity`, `used_total` FROM `sites`')
        
        for site_id, name, host, storage_type, backend, capacity, used_total in sites:
            site = Site(name, host = host, storage_type = Site.storage_type(storage_type), backend = backend, capacity = capacity, used_total = used_total)

            site_list[name] = site

            self._site_ids[site] = site_id

        datasets = self._query('SELECT `id`, `name`, `size`, `num_files`, `is_open` FROM `datasets`')

        for dataset_id, name, size, num_files, is_open in datasets:
            dataset_list[name] = Dataset(name, size = size, num_files = num_files, is_open = is_open)

            self.dataset_ids[name] = dataset_id

        blocks = {}
            
        blocks = self._query('SELECT ds.`name`, bl.`id`, bl.`name`, bl.`size`, bl.`num_files`, bl.`is_open` FROM `blocks` AS bl INNER JOIN `datasets` AS ds ON ds.`id` = bl.`dataset_id`')

        for dsname, blid, name, size, num_files, is_open in blocks:
            block = Block(name, size = size, num_files = num_files, is_open = is_open)
            block.dataset = dataset_list[dsname]

            blocks[blid] = block

        dataset_replicas = self._query('SELECT ds.`name`, st.`name`, rp.`is_partial`, rp.`is_custodial` FROM `dataset_replicas` AS rp INNER JOIN `datasets` AS ds ON ds.`id` = rp.`dataset_id` INNER JOIN `sites` AS st ON st.`id` = rp.`site_id`')

        for dsname, sitename, is_partial, is_custodial in dataset_replicas:
            dataset = dataset_list[dsname]
            site = site_list[sitename]

            rep = DatasetReplica(dataset, site, is_partial = is_partial, is_custodial = is_custodial)

            dataset.replicas.append(rep)
            site.datasets.append(dataset)

        block_replicas = self._query('SELECT bl.`id`, st.`name`, rp.`is_custodial`, UNIX_TIMESTAMP(rp.`time_created`), UNIX_TIMESTAMP(rp.`time_updated`) FROM `block_replicas` AS rp INNER JOIN `blocks` AS bl ON bl.`id` = rp.`block_id` INNER JOIN `sites` AS st ON st.`id` = rp.`site_id`')

        for blid, sitename, is_custodial, time_created, time_updated in block_replicas:
            block = blocks[blid]
            site = site_list[sitename]

            rep = BlockReplica(block, site, is_custodial = is_custodial, time_created = time_created, time_updated = time_updated)

            block.replicas.append(rep)
            site.blocks.append(block)

        return site_list, dataset_list

    def _do_save_data(self, site_list, dataset_list): #override

        def make_insert_query(table, fields):
            sql = 'INSERT INTO `' + table + '` (' + ','.join(['`{f}`'.format(f = f) for f in fields]) + ') VALUES %s'
            sql += ' ON DUPLICATE KEY UPDATE ' + ','.join(['`{f}`=VALUES(`{f}`)'.format(f = f) for f in fields])

            return sql

        # insert/update sites
        sql = make_insert_query('sites', ['name', 'host', 'storage_type', 'backend', 'capacity', 'used_total'])

        template = '(\'{name}\',\'{host}\',\'{storage_type}\',\'{backend}\',{capacity},{used_total})'
        mapping = lambda s: {'name': s.name, 'host': s.host, 'storage_type': Site.storage_type(s.storage_type), 'backend': s.backend, 'capacity': s.capacity, 'used_total': s.used_total}

        self._query_many(sql, template, mapping, site_list.values())

        # insert/update datasets
        sql = make_insert_query('datasets', ['name', 'size', 'num_files', 'is_open'])

        template = '(\'{name}\',{size},{num_files},{is_open})'
        mapping = lambda d: {'name': d.name, 'size': d.size, 'num_files': d.num_files, 'is_open': d.is_open}

        self._query_many(sql, template, mapping, dataset_list.values())

        dataset_ids = dict(self._query('SELECT `name`, `id` FROM `datasets`'))
        site_ids = dict(self._query('SELECT `name`, `id` FROM `sites`'))

        for ds_name, dataset in dataset_list.items():
            dataset_id = dataset_ids[ds_name]

            # insert/update dataset replicas
            sql = make_insert_query('dataset_replicas', ['dataset_id', 'site_id', 'is_partial', 'is_custodial'])

            template = '(%d,{site_id},{is_partial},{is_custodial})' % dataset_id
            mapping = lambda r: {'site_id': site_ids[r.site.name], 'is_partial': r.is_partial, 'is_custodial': r.is_custodial}

            self._query_many(sql, template, mapping, dataset.replicas)
            
            # deal with blocks only if dataset is partial on some site
            if len(filter(lambda r: r.is_partial, dataset.replicas)) != 0:
                continue
            
            # insert/update blocks
            sql = make_insert_query('blocks', ['name', 'dataset_id', 'size', 'num_files', 'is_open'])

            template = '(\'{name}\',%d,{size},{num_files},{is_open})' % dataset_id
            mapping = lambda b: {'name': b.name, 'size': b.size, 'num_files': b.num_files, 'is_open': b.is_open}

            self._query_many(sql, template, mapping, dataset.blocks)

            block_ids = dict(self._query('SELECT `name`, `id` FROM `blocks` WHERE `dataset_id` = %s', dataset_id))

            for block in dataset.blocks:
                block_id = block_ids[block.name]

                # insert/update block replicas
                sql = make_insert_query('block_replicas', ['block_id', 'site_id', 'is_custodial', 'time_created', 'time_updated'])

                template = '(%d,{site_id},{is_custodial},FROM_UNIXTIME({time_created}),FROM_UNIXTIME({time_updated}))' % block_id
                mapping = lambda r: {'site_id': site_ids[r.site.name], 'is_custodial': r.is_custodial, 'time_created': r.time_created, 'time_updated': r.time_updated}
    
                self._query_many(sql, template, mapping, block.replicas)

    def _query(self, sql, *args):
        cursor = self.connection.cursor()

        if config.debug_level > 1:
            print sql

        cursor.execute(sql, args)

        result = cursor.fetchall()

        if cursor.description is None:
            # insert query
            return cursor.lastrowid

        elif len(result) != 0 and len(result[0]) == 1:
            # single column requested
            return [row[0] for row in result]

        else:
            return list(result)

    def _query_many(self, sql, template, mapping, objects):
        cursor = self.connection.cursor()

        values = ''
        for obj in objects:
            if values:
                values += ','

            replacements = mapping(obj)
            values += template.format(**replacements)
            
            if len(values) > 1024 * 512:
                cursor.execute(sql % values)
                values = ''

        if config.debug_level > 1:
            print sql % values

        cursor.execute(sql % values)

        return cursor.fetchall()
