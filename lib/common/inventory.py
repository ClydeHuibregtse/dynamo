import logging
import fnmatch
import re

from common.interface.classes import default_interface
from common.interface.store import LocalStoreInterface
from common.dataformat import IntegrityError, Dataset, Site, DatasetReplica, BlockReplica
import common.configuration as config

logger = logging.getLogger(__name__)

class ConsistencyError(Exception):
    """Exception to be raised in case of data consistency problem."""
    
    pass

# Create partitions
Site.set_partitions(config.inventory.partitions)

class InventoryManager(object):
    """Bookkeeping class to bridge the communication between remote and local data sources."""

    def __init__(self, load_data = True, store_cls = None, site_source_cls = None, dataset_source_cls = None, replica_source_cls = None):
        if store_cls:
            self.store = store_cls()
        else:
            self.store = default_interface['store']()

        if site_source_cls:
            self.site_source = site_source_cls()
        else:
            self.site_source = default_interface['site_source']()

        if dataset_source_cls:
            self.dataset_source = dataset_source_cls()
        else:
            self.dataset_source = default_interface['dataset_source']()

        if replica_source_cls:
            self.replica_source = replica_source_cls()
        else:
            self.replica_source = default_interface['replica_source']()

        self.sites = {}
        self.groups = {}
        self.datasets = {}

        if load_data:
            self.load()

    def load(self, load_blocks = True, load_files = False, load_replicas = True, dataset_filter = '/*/*/*'):
        """
        Load information up to block level from local persistent storage to memory. The flag
        load_replicas can be used to determine whether dataset/block-site links should also be
        loaded; it is set to false when loading for an inventory update (link information is
        volatile).
        """

        logger.info('Loading data from local persistent storage.')

        self.sites = {}
        self.groups = {}
        self.datasets = {}
        
        self.store.acquire_lock()

        try:
            site_names = self.store.get_site_list(include = config.inventory.included_sites, exclude = config.inventory.excluded_sites)

            sites, groups, datasets = self.store.load_data(
                site_filt = site_names,
                dataset_filt = dataset_filter,
                load_blocks = load_blocks,
                load_files = load_files,
                load_replicas = load_replicas
            )

            self.sites = dict((s.name, s) for s in sites)
            self.groups = dict((g.name, g) for g in groups)
            self.datasets = dict((d.name, d) for d in datasets)

            num_dataset_replicas = 0
            num_block_replicas = 0

            if load_replicas:
                for dataset in self.datasets.values():
                    if dataset.replicas is None:
                        continue
    
                    num_dataset_replicas += len(dataset.replicas)
                    num_block_replicas += sum(len(r.block_replicas) for r in dataset.replicas)

        finally:
            self.store.release_lock()

        logger.info('Data is loaded to memory. %d sites, %d groups, %d datasets, %d dataset replicas, %d block replicas.\n', len(self.sites), len(self.groups), len(self.datasets), num_dataset_replicas, num_block_replicas)

    def update(self, dataset_filter = '/*/*/*', load_first = True, make_snapshot = True):
        """Query the dataSource and get updated information."""

        logger.info('Locking inventory.')

        # Lock the inventory
        self.store.acquire_lock()

        try:
            if make_snapshot:
                logger.info('Making a snapshot of inventory.')
                snapshot_tag = self.store.make_snapshot()

            if load_first and len(self.sites) == 0:
                logger.info('Loading data from local storage.')
                self.load(load_blocks = False, load_files = False, load_replicas = False, dataset_filter = dataset_filter)

            else:
                logger.info('Unlinking replicas.')
                self.unlink_all_replicas()

            self.site_source.get_site_list(self.sites, include = config.inventory.included_sites, exclude = config.inventory.excluded_sites)

            self.site_source.set_site_status(self.sites)

            self.site_source.get_group_list(self.groups, filt = config.inventory.included_groups)

            # First get information on all replicas in the system, possibly creating datasets / blocks along the way.
            if dataset_filter == '/*/*/*':
                self.replica_source.make_replica_links(self)
            else:
                self.replica_source.make_replica_links(self, dataset_filt = dataset_filter)

            open_datasets = filter(lambda d: d.status == Dataset.STAT_PRODUCTION, self.datasets.values())
            # Typically we enter this function with no file data loaded from store, so each open_dataset will have new File objects created.
            # However this does not lead to any slowdown since we download the full file information for each dataset anyway.
            self.dataset_source.set_dataset_details(open_datasets)

            for dataset in open_datasets:
                if dataset.status != Dataset.STAT_PRODUCTION:
                    # status changed
                    continue

                for cond in config.inventory.ignore_datasets:
                    if cond(dataset):
                        dataset.status = Dataset.STAT_IGNORED
                        break

            self.replica_source.find_tape_copies(self.datasets)

            logger.info('Saving data.')

            # Save inventory data to persistent storage
            # Datasets and groups with no replicas are removed
            self.store.save_data(self.sites.values(), self.groups.values(), self.datasets.values())

            if make_snapshot:
                logger.info('Removing the snapshot.')
                self.store.remove_snapshot(snapshot_tag)

        finally:
            # Lock is released even in case of unexpected errors
            self.store.release_lock(force = True)

    def load_blocks(self, dataset):
        """
        Load blocks of a dataset. Try InventoryStore first, and if no record is found, query the DatasetInfoSource.
        """

        self.store.load_blocks(dataset)
        if dataset.blocks is None:
            self.dataset_source.set_dataset_details([dataset])

    def load_files(self, dataset):
        """
        Load blocks of a dataset. Try InventoryStore first, and if no record is found, query the DatasetInfoSource.
        """

        self.store.load_files(dataset)
        if dataset.files is None:
            self.dataset_source.set_dataset_details([dataset])

    def find_block_of(self, fullpath):
        """
        Return the Block that the file belongs to. If no Block is in memory, returns None.
        """

        return self.store.find_block_of(fullpath, self.datasets)

    def unlink_datasetreplica(self, replica):
        """
        Remove link from datasets and sites to the replica. Don't remove the replica-to-dataset/site link;
        replica objects may be still being used in the program.
        """

        dataset = replica.dataset
        site = replica.site

        # Remove block replicas from the site
        for block_replica in replica.block_replicas:
            site.remove_block_replica(block_replica)

        site.dataset_replicas.remove(replica)
        dataset.replicas.remove(replica)

    def unlink_all_replicas(self):
        for dataset in self.datasets.values():
            dataset.replicas = None

        for site in self.sites.values():
            site.dataset_replicas.clear()
            site.clear_block_replicas()

    def add_dataset_to_site(self, dataset, site, group = None, blocks = None):
        """
        Create a new DatasetReplica object and return.
        """

        if dataset.replicas is None:
            # this would be a case where a dataset previously completely absent from the pool is added back, e.g. when staging a dataset from tape.
            dataset.replicas = []

        new_replica = DatasetReplica(dataset, site)

        dataset.replicas.append(new_replica)
        site.dataset_replicas.add(new_replica)

        if dataset.blocks is None:
            self.load_blocks(dataset)

        if blocks is None:
            blocks = dataset.blocks

        for block in blocks:
            block_replica = BlockReplica(block, site, group, is_complete = False, is_custodial = False, size = 0)
            new_replica.block_replicas.append(block_replica)
            site.add_block_replica(block_replica)

        return new_replica

    def add_block_to_site(self, block, site, group = None):
        """
        Create a new BlockReplica object and return.
        """

        dataset = block.dataset

        drep = None
        if dataset.replicas is None:
            # see note in add_dataset_to_site
            dataset.replicas = []
        else:
            drep = dataset.find_replica(site)

        if drep is None:
            drep = DatasetReplica(dataset, site)
    
            dataset.replicas.append(drep)
            site.dataset_replicas.add(drep)

        new_replica = BlockReplica(block, site, group, is_complete = False, is_custodial = False, size = 0)    
        drep.block_replicas.append(new_replica)
        site.add_block_replica(new_replica)

        return new_replica

    def scan_datasets(self, dataset_filter = '/*/*/*'):
        """
        Checks the information of existing datasets and save changes. Intended for an independent daemon process.
        """

        if len(self.datasets) == 0:
            # load_files = False -> new files will be created in set_dataset_details. Doesn't really slow anything down.
            self.load(load_blocks = False, load_files = False, load_replicas = False)

        if dataset_filter == '/*/*/*':
            open_datasets = filter(lambda d: d.status == Dataset.STAT_PRODUCTION or d.status == Dataset.STAT_VALID, self.datasets.values())
        else:
            regex = re.compile(fnmatch.translate(dataset_filter))
            open_datasets = filter(lambda d: (d.status == Dataset.STAT_PRODUCTION or d.status == Dataset.STAT_VALID) and regex.match(d.name), self.datasets.values())

        self.dataset_source.set_dataset_details(open_datasets)

        self.store.save_datasets(open_datasets)


if __name__ == '__main__':

    from argparse import ArgumentParser
    import common.interface.classes as classes

    parser = ArgumentParser(description = 'Inventory manager')

    parser.add_argument('command', metavar = 'COMMAND', nargs = '+', help = '(update|scan|list (datasets|sites)) [commands]')
    parser.add_argument('--store', '-i', metavar = 'CLASS', dest = 'store_cls', default = '', help = 'Store class to be used.')
    parser.add_argument('--site-source', '-s', metavar = 'CLASS', dest = 'site_source_cls', default = '', help = 'SiteInfoSourceInterface class to be used.')
    parser.add_argument('--dataset-source', '-t', metavar = 'CLASS', dest = 'dataset_source_cls', default = '', help = 'DatasetInfoSourceInterface class to be used.')
    parser.add_argument('--replica-source', '-r', metavar = 'CLASS', dest = 'replica_source_cls', default = '', help = 'ReplicaInfoSourceInterface class to be used.')
    parser.add_argument('--dataset', '-d', metavar = 'EXPR', dest = 'dataset', default = '/*/*/*', help = 'Limit operation to datasets matching the expression.')
    parser.add_argument('--site', '-e', metavar = 'SITE', dest = 'sites', nargs = '+', default = ['@disk'], help = 'Site names or aggregate names (@disk, @tape, @all) to include.')
    parser.add_argument('--no-load', '-L', action = 'store_true', dest = 'no_load',  help = 'Do not load the existing inventory when updating.')
    parser.add_argument('--no-snapshot', '-S', action = 'store_true', dest = 'no_snapshot',  help = 'Do not make a snapshot of existing inventory when updating.')
    parser.add_argument('--single-thread', '-T', action = 'store_true', dest = 'singleThread', help = 'Do not parallelize (for debugging).')
    parser.add_argument('--log-level', '-l', metavar = 'LEVEL', dest = 'log_level', default = '', help = 'Logging level.')

    args = parser.parse_args()

    if args.log_level:
        try:
            level = getattr(logging, args.log_level.upper())
            logging.getLogger().setLevel(level)
        except AttributeError:
            logging.warning('Log level ' + args.log_level + ' not defined')

    if args.singleThread:
        config.use_threads = False

    kwd = {'load_data': False} # not loading data by default to speed up update process

    for cls in ['store', 'site_source', 'dataset_source', 'replica_source']:
        clsname = getattr(args, cls + '_cls')
        if clsname == '':
            kwd[cls + '_cls'] = classes.default_interface[cls]
        else:
            kwd[cls + '_cls'] = getattr(classes, clsname)

    config.inventory.included_sites = []
    for pattern in args.sites:
        if pattern == '@all':
            config.inventory.included_sites = config.mss_sites + config.disk_sites
            break
        elif pattern == '@disk':
            config.inventory.included_sites = config.disk_sites
            break
        elif pattern == '@tape':
            config.inventory.included_sites = config.tape_sites
            break
        else:
            config.inventory.included_sites.append(pattern)

    manager = InventoryManager(**kwd)

    icmd = 0
    while icmd != len(args.command):
        command = args.command[icmd]
        icmd += 1
    
        if command == 'update':
            manager.update(dataset_filter = args.dataset, load_first = not args.no_load, make_snapshot = not args.no_snapshot)
    
        elif command == 'scan':
            manager.scan_datasets(dataset_filter = args.dataset)
    
        elif command == 'list':
            if len(manager.datasets) == 0:
                manager.load()
    
            target = args.command[icmd]
            icmd += 1
    
            if target == 'datasets':
                print manager.datasets.keys()
    
            elif target == 'sites':
                print manager.sites.keys()

        elif command == 'lastupdate':
            print manager.store.get_last_update()
