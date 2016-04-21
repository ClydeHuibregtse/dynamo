import logging

import common.configuration as config

logger = logging.getLogger(__name__)

class TransactionHistoryInterface(object):
    """
    Interface for transaction history. Has a locking mechanism similar to store.
    """

    def __init__(self):
        self._lock_depth = 0

    def acquire_lock(self):
        if self._lock_depth == 0:
            self._do_acquire_lock()

        self._lock_depth += 1

    def release_lock(self, force = False):
        if self._lock_depth == 1 or force:
            self._do_release_lock()

        if self._lock_depth > 0: # should always be the case if properly programmed
            self._lock_depth -= 1

    def make_copy_entry(self, site, operation_id, approved, ro_list, size):
        if config.read_only:
            logger.info('make_copy_entry')
            return

        self.acquire_lock()
        try:
            self._do_make_copy_entry(site, operation_id, approved, ro_list, size)
        finally:
            self.release_lock()

    def make_deletion_entry(self, site, operation_id, approved, datasets, size):
        if config.read_only:
            logger.info('make_deletion_entry')
            return

        self.acquire_lock()
        try:
            self._do_make_deletion_entry(site, operation_id, approved, datasets, size)
        finally:
            self.release_lock()

    def update_copy_entry(self, copy_record):
        """
        Update copy entry from the argument. Only certain fields (approved, completion_time) are updatable.
        """

        if config.read_only:
            logger.info('update_copy_entry')
            return

        self.acquire_lock()
        try:
            self._do_update_copy_entry(copy_record)
        finally:
            self.release_lock()

    def update_deletion_entry(self, deletion_record):
        """
        Update deletion entry from the argument. Only certain fields (approved, completion_time) are updatable.
        """

        if config.read_only:
            logger.info('update_deletion_entry')
            return

        self.acquire_lock()
        try:
            self._do_update_deletion_entry(deletion_record)
        finally:
            self.release_lock()

    def get_incomplete_copies(self):
        self.acquire_lock()
        try:
            # list of HistoryRecords
            copies = self._do_get_incomplete_copies()
        finally:
            self.release_lock()

        return copies

    def get_incomplete_deletions(self):
        self.acquire_lock()
        try:
            # list of HistoryRecords
            deletions = self._do_get_incomplete_deletions()
        finally:
            self.release_lock()

        return deletions


if __name__ == '__main__':

    import sys
    import time
    from argparse import ArgumentParser
    import common.interface.classes as classes

    parser = ArgumentParser(description = 'Local inventory store interface')

    parser.add_argument('command', metavar = 'COMMAND', help = '(update)')
    parser.add_argument('--class', '-c', metavar = 'CLASS', dest = 'class_name', default = '', help = 'TransactionHistoryInterface class to be used.')
    parser.add_argument('--copy-class', '-p', metavar = 'CLASS', dest = 'copy_class_name', default = '', help = 'CopyInterface class to be used.')
    parser.add_argument('--deletion-class', '-d', metavar = 'CLASS', dest = 'deletion_class_name', default = '', help = 'DeletionInterface class to be used.')

    args = parser.parse_args()
    sys.argv = []

    if args.class_name == '':
        interface = classes.default_interface['history']()
    else:
        interface = getattr(classes, args.class_name)()

    if args.command == 'update':

        if args.copy_class_name == '':
            copy_interface = classes.default_interface['copy']()
        else:
            copy_interface = getattr(classes, args.copy_class_name)()

        if args.deletion_class_name == '':
            deletion_interface = classes.default_interface['deletion']()
        else:
            deletion_interface = getattr(classes, args.deletion_class_name)()
        
        incomplete_copies = interface.get_incomplete_copies()
        
        for record in incomplete_copies:
            completed = copy_interface.check_completion(record.operation_id)
            if completed:
                logger.info('Copy %d to %s has completed.', record.operation_id, record.site_name)
        
                record.completion_time = time.time()
                interface.update_copy_entry(record)
        
        incomplete_deletions = interface.get_incomplete_deletions()
        
        for record in incomplete_deletions:
            completed = copy_interface.check_completion(record.operation_id)
            if completed:
                logger.info('Deletion %d at %s has completed.', record.operation_id, record.site_name)
        
                record.completion_time = time.time()
                interface.update_deletion_entry(record)