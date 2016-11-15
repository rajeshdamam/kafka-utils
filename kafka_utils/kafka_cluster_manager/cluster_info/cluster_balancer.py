# -*- coding: utf-8 -*-
# Copyright 2016 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import itertools
import logging
import shlex

from .util import separate_groups


class ClusterBalancer(object):
    """Interface that is used to implement any cluster partition balancing approach.

    :param cluster_topology: The ClusterTopology object that should be acted on.
    :param args: The program arguments.
    """

    def __init__(self, cluster_topology, args=None):
        self.cluster_topology = cluster_topology
        self.args = args
        if hasattr(args, 'balancer_args'):
            self.parse_args(list(itertools.chain.from_iterable(
                shlex.split(arg) for arg in args.balancer_args
            )))
        else:
            self.parse_args([])
        self.log = logging.getLogger(self.__class__.__name__)

    def parse_args(self, _blancer_args):
        """Parse partition measurer command line arguments.

        :param _balancer_args: The list of arguments as strings.
        """
        pass

    def rebalance(self):
        """Rebalance partitions across the brokers in the cluster."""
        raise NotImplementedError("Implement in subclass")

    def decommission_brokers(self, broker_ids):
        """Decommission a broker and balance all of its partitions across the cluster.

        :param broker_ids: A list of strings representing valid broker ids in the cluster.
        :raises InvalidBrokerIdError: A broker id is invalid.
        """
        raise NotImplementedError("Implement in subclass")

    def add_replica(self, partition_name, count=1):
        """Add replicas of a partition to the cluster, while maintaining the cluster's balance.

        :param partition_name: (topic_id, partition_id) of the partition to add replicas of.
        :param count: The number of replicas to add.

        :raises InvalidReplicationFactorError: The resulting replication factor is invalid.
        """
        raise NotImplementedError("Implement in subclass")

    def remove_replica(self, partition_name, osr_broker_ids, count=1):
        """Remove replicas of a partition from the cluster, while maintaining the cluster's balance.

        :param partition_name: (topic_id, partition_id) of the partition to remove replicas of.
        :param osr_broker_ids: A set of the partition's out-of-sync broker ids.
        :param count: The number of replicas to remove.

        :raises InvalidReplicationFactorError: The resulting replication factor is invalid.
        """
        raise NotImplementedError("Implement in subclass")

    def rebalance_replicas(
            self,
            max_movement_count=None,
            max_movement_size=None,
    ):
        """Balance replicas across replication-groups.

        :param max_movement_count: The maximum number of partitions to move.
        :param max_movement_size: The maximum total size of the partitions to move.

        :returns: A 2-tuple whose first element is the number of partitions moved
            and whose second element is the total size of the partitions moved.
        """
        movement_count = 0
        movement_size = 0
        for partition in self.cluster_topology.partitions.itervalues():
            count, size = self._rebalance_partition_replicas(
                partition,
                max_movement_count,
                max_movement_size
            )
            movement_count += count
            movement_size += size

        return movement_count, movement_size

    def _rebalance_partition_replicas(
            self,
            partition,
            max_movement_count=None,
            max_movement_size=None,
    ):
        """Rebalance replication groups for given partition."""
        # Separate replication-groups into under and over replicated
        total = partition.replication_factor
        over_replicated_rgs, under_replicated_rgs = separate_groups(
            self.cluster_topology.rgs.values(),
            lambda g: g.count_replica(partition),
            total,
        )

        # Move replicas from over-replicated to under-replicated groups
        movement_count = 0
        movement_size = 0
        while (
            under_replicated_rgs and over_replicated_rgs
        ) and (
            max_movement_size is None or
            movement_size + partition.size <= max_movement_size
        ) and (
            max_movement_count is None or
            movement_count < max_movement_count
        ):
            # Decide source and destination group
            rg_source = self._elect_source_replication_group(
                over_replicated_rgs,
                partition,
            )
            rg_destination = self._elect_dest_replication_group(
                rg_source.count_replica(partition),
                under_replicated_rgs,
                partition,
            )
            if rg_source and rg_destination:
                # Actual movement of partition
                self.log.debug(
                    'Moving partition {p_name} from replication-group '
                    '{rg_source} to replication-group {rg_dest}'.format(
                        p_name=partition.name,
                        rg_source=rg_source.id,
                        rg_dest=rg_destination.id,
                    ),
                )
                rg_source.move_partition(rg_destination, partition)
                movement_count += 1
                movement_size += partition.size
            else:
                # Groups balanced or cannot be balanced further
                break
            # Re-compute under and over-replicated replication-groups
            over_replicated_rgs, under_replicated_rgs = separate_groups(
                self.cluster_topology.rgs.values(),
                lambda g: g.count_replica(partition),
                total,
            )
        return movement_count, movement_size

    def _elect_source_replication_group(
            self,
            over_replicated_rgs,
            partition,
    ):
        """Decide source replication-group based as group with highest replica
        count.
        """
        return max(
            over_replicated_rgs,
            key=lambda rg: rg.count_replica(partition),
        )

    def _elect_dest_replication_group(
            self,
            replica_count_source,
            under_replicated_rgs,
            partition,
    ):
        """Decide destination replication-group based on replica-count."""
        min_replicated_rg = min(
            under_replicated_rgs,
            key=lambda rg: rg.count_replica(partition),
        )
        # Locate under-replicated replication-group with lesser
        # replica count than source replication-group
        if min_replicated_rg.count_replica(partition) < replica_count_source - 1:
            return min_replicated_rg
        return None
