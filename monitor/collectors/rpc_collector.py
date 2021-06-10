from __future__ import annotations

import asyncio
import logging
from asyncio import Queue
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Any, List

import aiohttp
from chia.rpc.farmer_rpc_client import FarmerRpcClient
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.rpc.harvester_rpc_client import HarvesterRpcClient
from chia.rpc.rpc_client import RpcClient
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.server.outbound_message import NodeType
from chia.util.config import load_config
from chia.util.default_root import DEFAULT_ROOT_PATH
from chia.util.ints import uint16
from monitor.collectors.collector import Collector
from monitor.events import (BlockchainStateEvent, ChiaEvent, ConnectionsEvent,
                            HarvesterPlotsEvent, WalletBalanceEvent)


class RpcCollector(Collector):
    full_node_client: FullNodeRpcClient
    wallet_client: WalletRpcClient
    harvester_client: HarvesterRpcClient
    farmer_client: FarmerRpcClient

    @staticmethod
    async def create(root_path: Path, net_config: Dict, event_queue: Queue[ChiaEvent]) -> RpcCollector:
        self = RpcCollector()
        self.log = logging.getLogger(__name__)
        self.event_queue = event_queue

        self_hostname = net_config["self_hostname"]
        full_node_rpc_port = net_config["full_node"]["rpc_port"]
        wallet_rpc_port = net_config["wallet"]["rpc_port"]
        harvester_rpc_port = net_config["harvester"]["rpc_port"]
        farmer_rpc_port = net_config["farmer"]["rpc_port"]

        self.full_node_client = await FullNodeRpcClient.create(self_hostname, uint16(full_node_rpc_port),
                                                               root_path, net_config)
        self.wallet_client = await WalletRpcClient.create(self_hostname, uint16(wallet_rpc_port),
                                                          root_path, net_config)
        self.harvester_client = await HarvesterRpcClient.create(self_hostname,
                                                                uint16(harvester_rpc_port), root_path,
                                                                net_config)
        self.farmer_client = await FarmerRpcClient.create(self_hostname, uint16(farmer_rpc_port),
                                                          root_path, net_config)
        return self

    async def get_wallet_balance(self) -> None:
        try:
            wallets = await self.wallet_client.get_wallets()
            confirmed_balances = []
            for wallet in wallets:
                balance = await self.wallet_client.get_wallet_balance(wallet["id"])
                confirmed_balances.append(balance["confirmed_wallet_balance"])
        except:
            raise ConnectionError("Failed to get wallet balance via RPC. Is your wallet running?")
        event = WalletBalanceEvent(ts=datetime.now(), confirmed=str(sum(confirmed_balances)))
        await self.publish_event(event)

    async def get_plots(self, harvester_rpc_port: int, harvester_rpc_hostname: str) -> Optional[Dict[str, Any]]:
        plots = None
        try:
            config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
            if harvester_rpc_hostname is None:
                harvester_rpc_hostname = config["self_hostname"]

            if harvester_rpc_port is None:
                harvester_rpc_port = config["harvester"]["rpc_port"]
            harvester_client = await HarvesterRpcClient.create(
                harvester_rpc_hostname, uint16(harvester_rpc_port), DEFAULT_ROOT_PATH, config
            )
            plots = await harvester_client.get_plots()
        except Exception as e:
            if isinstance(e, aiohttp.ClientConnectorError):
                print(
                    f"Connection error. Check if harvester is running at {harvester_rpc_hostname}:{harvester_rpc_port}")
            else:
                print(f"Exception from 'harvester' {e}")

        harvester_client.close()
        await harvester_client.await_closed()
        return plots

    async def get_all_plots(self, harvester_rpc_port: int, harvester_hosts: List[str]) -> Optional[Dict[str, Any]]:
        all_plots = None
        for host in harvester_hosts:
            plots = await self.get_plots(harvester_rpc_port, host)
            if plots is not None and plots["success"]:
                if all_plots is None:
                    all_plots = plots
                else:
                    all_plots["plots"] += plots["plots"]
        return all_plots

    async def get_harvesters(self, farmer_rpc_port: int) -> List[str]:
        harvester_hosts = []
        try:
            config = load_config(DEFAULT_ROOT_PATH, "config.yaml")
            self_hostname = config["self_hostname"]
            if farmer_rpc_port is None:
                farmer_rpc_port = config["farmer"]["rpc_port"]
            farmer_client = await FarmerRpcClient.create(self_hostname, uint16(farmer_rpc_port), DEFAULT_ROOT_PATH,
                                                         config)
            connections = await farmer_client.get_connections()
            for c in connections:
                if c["type"] == NodeType.HARVESTER:
                    harvester_hosts.append(c["peer_host"])
        except Exception as e:
            print(f"Exception from 'farmer' {e}")

        farmer_client.close()
        await farmer_client.await_closed()
        return harvester_hosts

    async def get_harvester_plots(self) -> None:
        try:
            # plots = await self.harvester_client.get_plots()
            harvesters = await self.get_harvesters(None)
            plots = await self.get_all_plots(None,harvesters)
        except:
            raise ConnectionError("Failed to get harvester plots via RPC. Is your harvester running?")
        event = HarvesterPlotsEvent(ts=datetime.now(),
                                    plot_count=len(plots["plots"]),
                                    plot_size=sum(plot["file_size"] for plot in plots["plots"]))
        await self.publish_event(event)

    async def get_blockchain_state(self) -> None:
        try:
            state = await self.full_node_client.get_blockchain_state()
        except:
            raise ConnectionError("Failed to get blockchain state via RPC. Is your full node running?")
        event = BlockchainStateEvent(ts=datetime.now(),
                                     space=str(state["space"]),
                                     diffculty=state["difficulty"],
                                     peak_height=str(state["peak"].height),
                                     synced=state["sync"]["synced"])
        await self.publish_event(event)

    async def get_connections(self) -> None:
        peers = await self.full_node_client.get_connections()
        full_node_connections = [peer for peer in peers if NodeType(peer["type"]) == NodeType.FULL_NODE]
        farmer_connections = [peer for peer in peers if NodeType(peer["type"]) == NodeType.FARMER]
        wallet_connections = [peer for peer in peers if NodeType(peer["type"]) == NodeType.WALLET]
        event = ConnectionsEvent(ts=datetime.now(),
                                 full_node_count=len(full_node_connections),
                                 farmer_count=len(farmer_connections),
                                 wallet_count=len(wallet_connections))
        await self.publish_event(event)

    async def task(self) -> None:
        while True:
            await asyncio.gather(self.get_wallet_balance(), self.get_harvester_plots(),
                                 self.get_blockchain_state(), self.get_connections())
            await asyncio.sleep(10)

    @staticmethod
    async def close_rpc_client(rpc_client: RpcClient) -> None:
        rpc_client.close()
        await rpc_client.await_closed()

    async def close(self) -> None:
        await RpcCollector.close_rpc_client(self.full_node_client)
        await RpcCollector.close_rpc_client(self.wallet_client)
        await RpcCollector.close_rpc_client(self.harvester_client)
        await RpcCollector.close_rpc_client(self.farmer_client)
