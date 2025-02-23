from .alchemy import AlchemyRPCClient
from .getblock import GetBlockRPCClient
from .infura import InfuraRPCClient
from .quicknode import QuickNodeRPCClient
from .fullnode import EthereumRPCClient
from ...generated import wallet_pb2
from ...errors import NetworkException

class EthereumAPIClient:
    """ Load balancer for all LTC address providers provided to an instance of this class,
        using the round robin scheduling algorithm.
    """

    def __init__(self, providers: bytes, addresses, max_cycles=100,
                 transactions=None, fullnode_endpoints=None, alchemy_token=None, getblock_token=None,
                 infura_token=None, quicknode_name=None, quicknode_token=None):
        provider_bitmask = int.from_bytes(providers, 'big')
        self.provider_list = []
        self.current_index = 0
        self.addresses = addresses
        self.max_cycles = max_cycles

        # Set everything to an empty list so that providers do not immediately start fetching
        # transactions and to avoid exceptions in loops later in this method.
        if not transactions:
            transactions = []
        if not esplora_endpoints:
            esplora_endpoints = []
        if not fullnode_endpoints:
            fullnode_endpoints = []
        if not fullnode_passprotected_endpoints:
            fullnode_passprotected_endpoints = []

        self.transactions = transactions

        if provider_bitmask & 1 << wallet_pb2.ETH_ALCHEMY + 1:
            self.provider_list.append(AlchemyRPCClient(addresses, alchemy_token, transactions=transactions))
        if provider_bitmask & 1 << wallet_pb2.ETH_FULLNODE + 1:
            for endpoint in fullnode_endpoints:
                self.provider_list.append(EthereumRPCClient(addresses, endpoint, transactions=transactions))
        if provider_bitmask & 1 << wallet_pb2.ETH_GETBLOCK + 1:
            self.provider_list.append(GetBlockRPCClient(addresses, getblock_token, transactions=transactions))
        if provider_bitmask & 1 << wallet_pb2.ETH_INFURA + 1:
            self.provider_list.append(InfuraRPCClient(addresses, infura_token, transactions=transactions))
        if provider_bitmask & 1 << wallet_pb2.ETH_ALCHEMY + 1:
            self.provider_list.append(QuickNodeRPCClient(addresses, quicknode_name, quicknode_token, transactions=transactions))

        
        self.get_transaction_history()

    def get_balance(self):
        """
        Retrieves the balance of the Ethereum address.

        Returns:
            float: The balance of the Ethereum address in LTC.

        Raises:
            Exception: If the API request fails or the address balance cannot be retrieved.
        """
        utxos = self.get_utxos()
        total_balance = 0
        confirmed_balance = 0
        for utxo in utxos:
            total_balance += utxo["amount"]
            # Careful: Block height #0 is the Genesis block - don't want to exclude that.
            if utxo["confirmed"]:
                confirmed_balance += utxo["amount"]
        return total_balance, confirmed_balance
        
    def get_utxos(self):
        # Transactions are generated in reverse order
        utxos = []
        for i in range(len(self.transactions)-1, -1, -1):
            for out in self.transactions[i].outputs:
                if out.spent:
                    continue
                if out.address in self.addresses:
                    utxo = {}
                    utxo["address"] = out.address
                    utxo["txid"] = self.transactions[i].txid
                    utxo["index"] = out.index
                    utxo["amount"] = out.amount
                    utxo["height"] = self.transactions[i].height
                    utxo["confirmed"] = self.transactions[i].confirmed
                    utxos.append(utxo)
        return utxos


    def advance_to_next_provider(self):
        if not self.provider_list:
            return
        
        self.current_index = (self.current_index + 1) % len(self.provider_list)
    
    def get_transaction_history(self):
        ntransactions = -1  # Set to invalid value for the first iteration
        last_transaction = None if len(self.transactions) == 0 else self.transactions[-1]
        cycle = 1
        while ntransactions != len(self.transactions):
            if cycle > self.max_cycles:
                raise NetworkException(f"None of the address providers are working after f{self.max_cycles} tries")
            ntransactions = len(self.transactions)
            self.provider_list[self.current_index].transactions = self.transactions
            try:
                self.provider_list[self.current_index].get_transaction_history(txhash=last_transaction)
                self.transactions = self.provider_list[self.current_index].transactions
                break
            except NetworkException:
                self.transactions = self.provider_list[self.current_index].transactions
                last_transaction = None if len(self.transactions) == 0 else self.transactions[-1]
                self.advance_to_next_provider()
                cycle += 1
    