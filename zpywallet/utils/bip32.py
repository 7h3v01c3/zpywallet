"""
Module for generating Heirarchical Deterministic (HD) keys for supported networks.
"""
from binascii import hexlify, unhexlify
from hashlib import sha256, sha512
import hmac
from os import urandom
import time

import coincurve

from .base58 import b58encode_check, b58decode_check
from ..mnemonic.mnemonic import Mnemonic
from .keys import PrivateKey, PublicKey, Point, secp256k1
from ..errors import incompatible_network_bytes_exception_factory, InvalidChildException, \
    InvalidPathError, WatchOnlyWalletError, SegwitError, unsupported_feature_exception_factory
from .utils import ensure_bytes, ensure_str, hash160, is_hex_string, long_to_hex

# import all the networks
from ..network import BitcoinSegwitMainNet



class HDWallet(object):
    """A BIP32 wallet is made up of Wallet nodes.

    A Private node contains both a public and private key, while a public
    node contains only a public key.

    **WARNING**:

    When creating a NEW wallet you MUST back up the private key. If
    you don't then any coins sent to your address will be LOST FOREVER.

    You need to save the private key somewhere. It is OK to just write
    it down on a piece of paper! Don't share this key with anyone!

    >>> my_wallet = Wallet.from_mnemonic(
    ...     key='correct horse battery staple')
    >>> private = my_wallet.serialize(private=True)
    >>> private  # doctest: +ELLIPSIS
    u'xprv9s21ZrQH143K2mDJW8vDeFwbyDbFv868mM2Zr87rJSTj8q16Unkaq1pryiV...'

    If you want to use this wallet on your website to accept bitcoin or
    altcoin payments, you should first create a primary child.

    BIP32 Hierarchical Deterministic Wallets are described in this BIP:
    https://github.com/bitcoin/bips/blob/master/bip-0032.mediawiki
    """

    def __init__(self,
                 chain_code,
                 depth=0,
                 parent_fingerprint=0,
                 child_number=0,
                 private_exponent=None,
                 private_key=None,
                 public_pair=None,
                 public_key=None,
                 mnemonic=None,
                 network="btc"):
        """Construct a new BIP32 compliant wallet.

        You probably don't want to use this init methd. Instead use one
        of the class methods for creating a wallet.

        In particular, the mnemonic is not used to generate the wallet in
        this constructor and its only purpose here is for storage. The Wallet
        object deals with master private keys for creating things, which are generated
        in the class methods.
        """

        if (not (private_exponent or private_key) and
                not (public_pair or public_key)):
            raise ValueError(
                "You must supply one of private_exponent or public_pair")

        self.mnemonic = mnemonic
        self.private_key = None
        self.public_key = None
        if private_key:
            self.private_key = private_key
        elif private_exponent:
            self.private_key = PrivateKey.from_int(private_exponent)

        if public_key:
            self.public_key = public_key
        elif public_pair:
            self.public_key = PublicKey.from_point(public_pair)
        else:
            self.public_key = self.private_key.public_key

        if (self.private_key and self.private_key.public_key !=
                self.public_key):
            raise ValueError(
                "Provided private and public values do not match")

        def hex_check_length(val, hex_len):
            if isinstance(val, int):
                return long_to_hex(val, hex_len)
            elif (isinstance(val, str) or
                    isinstance(val, bytes)) and is_hex_string(val):
                val = ensure_bytes(val)
                if len(val) != hex_len:
                    raise ValueError("Invalid parameter length")
                return val
            else:
                raise ValueError("Invalid parameter type")

        def hex_int(val):
            if isinstance(val, int):
                return int(val)
            elif (isinstance(val, str) or
                    isinstance(val, bytes)):
                val = ensure_bytes(val)
                if not is_hex_string(val):
                    val = hexlify(val)
                return int(val, 16)
            else:
                raise ValueError("parameter must be an int or long")

        self.network = network
        self.depth = hex_int(depth)
        if (isinstance(parent_fingerprint, str) or
                isinstance(parent_fingerprint, bytes)):
            val = ensure_bytes(parent_fingerprint)
            if val.startswith(b"0x"):
                parent_fingerprint = val[2:]
        self.parent_fingerprint = b"0x" + hex_check_length(parent_fingerprint, 8)
        self.child_number = hex_int(child_number)
        self.chain_code = hex_check_length(chain_code, 64)

    def get_private_key_hex(self):
        """
        Get the hex-encoded (I guess SEC1?) representation of the private key.

        DO NOT share this private key with anyone.
        """
        return ensure_bytes(self.private_key.to_hex())

    def get_public_key_hex(self, compressed=True):
        """Get the sec1 representation of the public key."""
        return ensure_bytes(self.public_key.to_hex(compressed))

    @property
    def identifier(self):
        """Get the identifier for this node.

        Extended keys can be identified by the Hash160 (RIPEMD160 after SHA256)
        of the public key's `key`. This corresponds exactly to the data used in
        traditional Bitcoin addresses. It is not advised to represent this data
        in base58 format though, as it may be interpreted as an address that
        way (and wallet software is not required to accept payment to the chain
        key itself).
        """
        key = self.get_public_key_hex()
        return ensure_bytes(hexlify(hash160(unhexlify(key))))

    @property
    def mnemonic_phrase(self):
        """Returns the mnemonic phrase for this wallet, if specified.
        
        WARNING: Never share your mnemonic phrase with anyone. They can
        use it to steal your assets.
        """
        return self.mnemonic

    @property
    def fingerprint(self):
        """The first 32 bits of the identifier are called the fingerprint."""
        # 32 bits == 4 Bytes == 8 hex characters
        return b'0x' + self.identifier[:8]

    def create_new_address_for_user(self, user_id):
        """Create a new bitcoin address to accept payments for a User.

        This is a convenience wrapper around `get_child` that helps you do
        the right thing. This method always creates a public, non-prime
        address that can be generated from a BIP32 public key on an
        insecure server."""
        max_id = 0x80000000
        if user_id < 0 or user_id > max_id:
            raise ValueError(f"Invalid UserID. Must be between 0 and {max_id}")
        return self.get_child(user_id, is_prime=False, as_private=False)

    def get_child_for_path(self, path):
        """Get a child for a given path.

        Rather than repeated calls to get_child, children can be found
        by a derivation path. Paths look like:

            m/0/1'/10

        Which is the same as

            self.get_child(0).get_child(-1).get_child(10)

        Or, in other words, the 10th publicly derived child of the 1st
        privately derived child of the 0th publicly derived child of master.

        You can use either ' or p to denote a prime (that is, privately
        derived) child.

        A child that has had its private key stripped can be requested by
        either passing a capital M or appending '.pub' to the end of the path.
        These three paths all give the same child that has had its private
        key scrubbed:

            M/0/1
            m/0/1.pub
            M/0/1.pub
        """
        path = ensure_str(path)

        if not path:
            raise InvalidPathError(f"{path} is not a valid path")

        # Figure out public/private derivation
        as_private = True
        if path.startswith("M"):
            as_private = False
        if path.endswith(".pub"):
            as_private = False
            path = path[:-4]

        parts = path.split("/")
        if len(parts) == 0:
            raise InvalidPathError()

        child = self
        for part in parts:
            if part.lower() == "m":
                continue
            is_prime = None  # Let primeness be figured out by the child number
            if part[-1] in "'p":
                is_prime = True
                part = part.replace("'", "").replace("p", "")
            try:
                child_number = int(part)
            except ValueError as exc:
                raise InvalidPathError(f"{path} is not a valid path") from exc
            child = child.get_child(child_number, is_prime)
        if not as_private:
            return child.public_copy()
        return child

    def legacy_child(self):
        """Equivalent to get_child(44, is_prime=True)
        """
        return self.get_child(44, is_prime=True)

    def segwit_child(self):
        """Equivalent to get_child(84, is_prime=True)
        """
        return self.get_child(84, is_prime=True)

    def get_child(self, child_number, is_prime=None, as_private=True):
        """Derive a child key.

        :param child_number: The number of the child key to compute
        :type child_number: int
        :param is_prime: If True, the child is calculated via private
            derivation. If False, then public derivation is used. If None,
            then it is figured out from the value of child_number.
        :type is_prime: bool, defaults to None
        :param as_private: If True, strips private key from the result.
            Defaults to False. If there is no private key present, this is
            ignored.
        :type as_private: bool

        Positive child_numbers (less than 2,147,483,648) produce publicly
        derived children.

        Negative numbers (greater than -2,147,483,648) uses private derivation.

        NOTE: Python can't do -0, so if you want the privately derived 0th
        child you need to manually set is_prime=True.

        NOTE: negative numbered children are provided as a convenience
        because nobody wants to remember the above numbers. Negative numbers
        are considered 'prime children', which is described in the BIP32 spec
        as a leading 1 in a 32 bit unsigned int.

        This derivation is fully described at
        https://github.com/bitcoin/bips/blob/master/bip-0032.mediawiki#child-key-derivation-functions
        """
        boundary = 0x80000000

        # Note: If this boundary check gets removed, then children above
        # the boundary should use private (prime) derivation.
        if abs(child_number) >= boundary:
            raise InvalidPathError(f"Invalid child number {child_number}")

        # If is_prime isn't set, then we can infer it from the child_number
        if is_prime is None:
            # Prime children are either < 0 or > 0x80000000
            if child_number < 0:
                child_number = abs(child_number)
                is_prime = True
            else:
                is_prime = False
        else:
            # Otherwise is_prime is set so the child_number should be between
            # 0 and 0x80000000
            if child_number < 0 or child_number >= boundary:
                raise ValueError(f"Invalid child number. Must be between 0 and {boundary}")

        if not self.private_key and is_prime:
            raise WatchOnlyWalletError(
                "Cannot compute a prime child without a private key")

        if is_prime:
            # Even though we take child_number as an int < boundary, the
            # internal derivation needs it to be the larger number.
            child_number = child_number + boundary
        child_number_hex = long_to_hex(child_number, 8)

        if is_prime:
            # Let data = concat(0x00, self.key, child_number)
            data = b'00' + ensure_bytes(self.private_key.to_hex())
        else:
            data = self.get_public_key_hex()
        data += child_number_hex

        # Compute a 64 Byte I that is the HMAC-SHA512, using self.chain_code
        # as the seed, and data as the message.
        ichild = hmac.new(
            unhexlify(ensure_bytes(self.chain_code)),
            msg=unhexlify(ensure_bytes(data)),
            digestmod=sha512).digest()
        # Split I into its 32 Byte components.
        ichild_left, ichild_right = ichild[:32], ichild[32:]

        if int(hexlify(ichild_left), 16) >= secp256k1.N:
            raise InvalidPathError("The derived key is too large.")

        c_i = hexlify(ichild_right)
        private_exponent = None
        point = None
        if self.private_key:
            # Use private information for derivation
            # I_L is added to the current key's secret exponent (mod n), where
            # n is the order of the ECDSA curve in use.
            private_exponent = (
                (int(hexlify(ichild_left), 16) +
                 int(ensure_bytes(self.private_key.to_hex()), 16))
                % secp256k1.N)
            # I_R is the child's chain code
        else:
            # Only use public information for this derivation
            gen = coincurve.PublicKey.from_point(secp256k1.Gx, secp256k1.Gy)
            point = Point(*gen.multiply(ichild_left).add(self.public_key.to_bytes()).point())
            # I_R is the child's chain code

        child = self.__class__(
            chain_code=c_i,
            depth=self.depth + 1,  # we have to go deeper...
            parent_fingerprint=self.fingerprint,
            child_number=child_number_hex,
            private_exponent=private_exponent,
            public_pair=point,
            network=self.network)
        if not as_private:
            return child.public_copy()
        return child

    def public_copy(self):
        """Clone this wallet and strip it of its private information."""
        return self.__class__(
            chain_code=self.chain_code,
            depth=self.depth,
            parent_fingerprint=self.parent_fingerprint,
            child_number=self.child_number,
            public_pair=self.public_key.to_point(),
            network=self.network)

    def crack_private_key(self, child_private_key):
        """Crack the parent private key given a child private key.

        BIP32 has a vulnerability/feature that allows you to recover the
        master private key if you're given a master public key and any of its
        publicly-derived child private keys. This is a pretty serious security
        vulnerability that looks as innocuous as this:

        >>> w = Wallet.new_random_wallet()
        >>> child = w.get_child(0, is_prime=False)
        >>> w_pub = w.public_copy()
        >>> assert w_pub.private_key is None
        >>> master_public_key = w_pub.serialize_b58(private=False)
        >>> # Now you put master_public_key on your website
        >>> # and give somebody a private key
        >>> public_master = Wallet.deserialize(master_public_key)
        >>> cracked_private_master = public_master.crack_private_key(child)
        >>> assert w == cracked_private_master  # :(

        Implementation details from:
          http://bitcoinmagazine.com/8396/deterministic-wallets-advantages-flaw/
        """

        if child_private_key.parent_fingerprint != self.fingerprint:
            raise InvalidChildException("This is not a valid child")
        if child_private_key.child_number >= 0x80000000:
            raise InvalidChildException(
                "Cannot crack private keys from private derivation")

        # Duplicate the public child derivation
        child_number_hex = long_to_hex(child_private_key.child_number, 8)
        data = self.get_public_key_hex() + child_number_hex
        ichild = hmac.new(
            unhexlify(self.chain_code),
            msg=unhexlify(data),
            digestmod=sha512).digest()
        ichild_left, _ = ichild[:32], ichild[32:]
        # Public derivation is the same as private derivation plus some offset
        # knowing the child's private key allows us to find this offset just
        # by subtracting the child's private key from the parent I_L data
        privkey = PrivateKey.from_bytes(ichild_left)
        parent_private_key = child_private_key.private_key - privkey
        return self.__class__(
            chain_code=self.chain_code,
            depth=self.depth,
            parent_fingerprint=self.parent_fingerprint,
            child_number=self.child_number,
            private_key=parent_private_key,
            network=self.network)

    def serialize(self, private=True, segwit=False):
        """Serialize this key.

        :param private: Whether or not the serialized key should contain
            private information. Set to False for a public-only representation
            that cannot spend funds but can create children. You want
            private=False if you are, for example, running an e-commerce
            website and want to accept bitcoin payments. See the README
            for more information.
        :param segwit: Whether to use segwit extended version bytes instead of
            legacy extended version bytes. Only for networks which support Segwit,
            therefore the default value is False.
        :type private: bool, defaults to True

        See the spec in `deserialize` for more details.
        """
        if private:
            if not self.private_key:
                raise WatchOnlyWalletError("Private key is not available")
            elif segwit:
                if not self.network.EXT_SEGWIT_SECRET_KEY:
                    raise SegwitError("Segwit is not supported on this network")
                network_version = long_to_hex(
                    self.network.EXT_SEGWIT_SECRET_KEY, 8)
            else:
                if not self.network.EXT_SECRET_KEY:
                    raise unsupported_feature_exception_factory(self.network.NAME, "private key serialization")
                network_version = long_to_hex(
                    self.network.EXT_SECRET_KEY, 8)
        else:
            if segwit:
                if not self.network.EXT_SEGWIT_PUBLIC_KEY:
                    raise SegwitError("Segwit is not supported on this network")
                network_version = long_to_hex(
                    self.network.EXT_SEGWIT_PUBLIC_KEY, 8)
            else:
                if not self.network.EXT_PUBLIC_KEY:
                    raise unsupported_feature_exception_factory(self.network.NAME, "public key serialization")
                network_version = long_to_hex(
                    self.network.EXT_PUBLIC_KEY, 8)
        depth = long_to_hex(self.depth, 2)
        parent_fingerprint = self.parent_fingerprint[2:]  # strip leading 0x
        child_number = long_to_hex(self.child_number, 8)
        chain_code = self.chain_code
        ret = (network_version + depth + parent_fingerprint + child_number +
               chain_code)
        # Private and public serializations are slightly different
        if private:
            ret += b'00' + ensure_bytes(self.private_key.to_hex())
        else:
            ret += self.get_public_key_hex(compressed=True)
        return ensure_bytes(ret.lower())

    def serialize_b58(self, private=True, segwit=False):
        """Encode the serialized node in base58."""
        return ensure_str(
            b58encode_check(unhexlify(self.serialize(private, segwit))))

    def address(self, compressed=True, witness_version=0):
        """Create a public address from this Wallet.

        Public addresses can accept payments.

        https://en.bitcoin.it/wiki/Technical_background_of_Bitcoin_addresses


        Args:
            compressed (bool): Whether or not the compressed key should
               be used.
            witness_version (int): Used only when creating Bech32 addresses.
                Allowed values are 0 (segwit) and 1 (Taproot).

        Returns:
            str: An encoded address
        """
        key = PublicKey.from_bytes(unhexlify(self.get_public_key_hex()), network=self.network)
        return ensure_str(key.address(compressed=compressed,
                                      witness_version=witness_version))

    @classmethod
    def deserialize(cls, key, network="btc"):
        """Load an extended BIP32 private key from a hex string.

        The key consists of

            * 4 byte version bytes (network key)
            * 1 byte depth:
                - 0x00 for master nodes,
                - 0x01 for level-1 descendants, ....
            * 4 byte fingerprint of the parent's key (0x00000000 if master key)
            * 4 byte child number. This is the number i in x_i = x_{par}/i,
              with x_i the key being serialized. This is encoded in MSB order.
              (0x00000000 if master key)
            * 32 bytes: the chain code
            * 33 bytes: the public key or private key data
              (0x02 + X or 0x03 + X for public keys, 0x00 + k for private keys)
              (Note that this also supports 0x04 + X + Y uncompressed points,
              but this is totally non-standard and this library won't even
              generate such data.)
        """

        if len(key) in [78, (78 + 32)]:
            # we have a byte array, so pass
            pass
        else:
            key = ensure_bytes(key)
            if len(key) in [78 * 2, (78 + 32) * 2]:
                # we have a hexlified non-base58 key, continue!
                key = unhexlify(key)
            elif len(key) == 111:
                # We have a base58 encoded string
                key = b58decode_check(key)
        # Now that we double checkd the values, convert back to bytes because
        # they're easier to slice
        version, depth, parent_fingerprint, child, chain_code, key_data = (
            key[:4], key[4], key[5:9], key[9:13], key[13:45], key[45:])

        if int(depth) == 0 and int(hexlify(parent_fingerprint), 16) != 0:
            raise ValueError("Zero depth with non-zero parent fingerprint")
        if int(depth) == 0 and int(hexlify(child)) != 0:
            raise ValueError("Zero depth with non-zero index")
        version_long = int(hexlify(version), 16)
        exponent = None
        pubkey = None
        point_type = key_data[0]
        if not isinstance(point_type, int):
            point_type = ord(point_type)
        if point_type == 0:
            # Private key
            if version_long != network.EXT_SECRET_KEY:
                raise incompatible_network_bytes_exception_factory(
                    network.NAME, unhexlify(f"{network.EXT_SECRET_KEY:x}".zfill(8)),
                    version)
            exponent = key_data[1:]
            iexponent = int(hexlify(exponent), 16)
            if iexponent < 1 or iexponent >= secp256k1.N:
                raise ValueError("Private key is out of range")
        elif point_type in [2, 3, 4]:
            # Compressed public coordinates
            if version_long != network.EXT_PUBLIC_KEY:
                raise incompatible_network_bytes_exception_factory(
                    network.NAME, unhexlify(f"{network.EXT_PUBLIC_KEY:x}".zfill(8)),
                    version)
            pubkey = PublicKey.from_bytes(key_data)
            # Even though this was generated from a compressed pubkey, we
            # want to store it as an uncompressed pubkey
            pubkey.compressed = False
        else:
            raise ValueError(f"Invalid key_data prefix, got {point_type}")

        def bytes_int(byte_seq):
            if byte_seq is None:
                return byte_seq
            elif isinstance(byte_seq, int):
                return byte_seq
            return int(hexlify(byte_seq), 16)

        return cls(depth=bytes_int(depth),
                   parent_fingerprint=bytes_int(parent_fingerprint),
                   child_number=bytes_int(child),
                   chain_code=bytes_int(chain_code),
                   private_exponent=bytes_int(exponent),
                   public_key=pubkey,
                   network=network)

    @classmethod
    def from_mnemonic(cls, mnemonic, passphrase='', network=BitcoinSegwitMainNet):
        """Generate a new PrivateKey from a secret key.

        :param mnemonic: The key to use to generate this wallet.
        :param passphrase: An optional passphrase for this mnemonic.
        :param network: The network to use. Defaults to Bitcoin mainnet.
        

        See https://github.com/bitcoin/bips/blob/master/bip-0032.mediawiki#Serialization_format
        """
        mne = Mnemonic(language='english')
        seed = ensure_bytes(mne.to_seed(mnemonic, passphrase))

        # Given a seed S of at least 128 bits, but 256 is advised
        # Calculate I = HMAC-SHA512(key="Bitcoin seed", msg=S)
        I = hmac.new(b"Bitcoin seed", msg=seed, digestmod=sha512).digest()
        # Split I into two 32-byte sequences, IL and IR.
        Il, Ir = I[:32], I[32:]
        # Use IL as master secret key, and IR as master chain code.
        return cls(private_exponent=int(hexlify(Il), 16),
                   chain_code=hexlify(Ir), mnemonic=mnemonic,
                   network=network)

    @classmethod
    def from_brainwallet(cls, password, network=BitcoinSegwitMainNet):
        """
        Generate a new key from a password using 50,000 rounds of HMAC-SHA256.

        This should generate the same result as bip32.org.

        WARNING: The security of this method has not been evaluated.

        Args:
            :param password (str):  The value to hash for generating the wallet.
            It may be a long string. Do not use a phrase from a book or song,
            as that will be guessed and is not secure.
            :param network: The network to use. Defaults to Bitcoin mainnet.
        
        Returns:
            Wallet: A Wallet object.
        """
        # Make sure the password string is bytes
        key = ensure_bytes(password)
        data = unhexlify(b"0" * 64)  # 256-bit 0
        for _ in range(50000):
            data = hmac.new(key, msg=data, digestmod=sha256).digest()

        I = hmac.new(b"Bitcoin seed", msg=data, digestmod=sha512).digest()
        # Split I into two 32-byte sequences, IL and IR.
        Il, Ir = I[:32], I[32:]
        # Use IL as master secret key, and IR as master chain code.
        return cls(private_exponent=int(hexlify(Il), 16),
                   chain_code=hexlify(Ir),
                   network=network)

    @classmethod
    def from_master_seed(cls, seed, network=BitcoinSegwitMainNet):
        """Generate a new PrivateKey from a seed (byte string).

        :param seed: The byte sequence to use to generate this wallet. The seed length
            should be at least 128 bits, no longer than 256 bits, and be divisible by 32.
        :param network: The network to use. Defaults to Bitcoin mainnet.
        

        See https://github.com/bitcoin/bips/blob/master/bip-0032.mediawiki#Serialization_format
        """

        # Given a seed S of at least 128 bits, but 256 is advised
        # Calculate I = HMAC-SHA512(key="Bitcoin seed", msg=S)
        I = hmac.new(b"Bitcoin seed", msg=seed, digestmod=sha512).digest()
        # Split I into two 32-byte sequences, IL and IR.
        Il, Ir = I[:32], I[32:]
        # Use IL as master secret key, and IR as master chain code.
        return cls(private_exponent=int(hexlify(Il), 16),
                   chain_code=hexlify(Ir),
                   network=network)

    @classmethod
    def from_random(cls, passphrase='', strength=128, network=BitcoinSegwitMainNet):
        """ Generates a master key from system entropy.

        Args:
            :param strength (int): Amount of entropy desired, in bits.
                This should be a multiple of 32 between 128 and 256.
                It directly affects the length of the mnemonic exported
                (each additional 32 bits adds an extra three words at the end).
            passphrase (str): An optional passphrase for the generated
               mnemonic string.
            :param network: The network to use for things like defining key
                key paths and supported address formats. Defaults to Bitcoin mainnet.

        Returns:
            Wallet: The wallet object created.
        """
        if strength % 32 != 0:
            raise ValueError("strength must be a multiple of 32")
        if strength < 128 or strength > 256:
            raise ValueError("strength should be >= 128 and <= 256")
        entropy = urandom(strength // 8)
        mne = Mnemonic(language='english')
        mnemonic = mne.to_mnemonic(entropy)
        return cls.from_mnemonic(
            mnemonic, passphrase, network=network)


    def __eq__(self, other):
        attrs = [
            'chain_code',
            'depth',
            'parent_fingerprint',
            'child_number',
            'private_key',
            'public_key',
            'network',
        ]
        return other and all(
            getattr(self, attr) == getattr(other, attr) for attr in attrs)

    def __ne__(self, other):
        return not self == other

    __hash__ = object.__hash__

    @classmethod
    def new_random_wallet(cls, user_entropy=None, network=BitcoinSegwitMainNet):
        """
        Generate a new wallet using a randomly generated 512 bit seed.

        Args:
            user_entropy: Optional user-supplied entropy which is combined
                combined with the random seed, to help counteract compromised
                PRNGs.

        You are encouraged to add an optional `user_entropy` string to protect
        against a compromised CSPRNG. This will be combined with the output
        from the CSPRNG. Note that if you do supply this value it only adds
        additional entropy and will not be sufficient to recover the random
        wallet. If you're even saving `user_entropy` at all, you're doing it
        wrong.
        """
        seed = str(urandom(64))  # 512/8
        # weak extra protection inspired by pybitcointools implementation:
        seed += str(int(time.time()*10**6))
        if user_entropy:
            user_entropy = str(user_entropy)  # allow for int/long
            seed += user_entropy
        return cls.from_mnemonic(seed, network=network)
