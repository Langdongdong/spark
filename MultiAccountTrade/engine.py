import logging, pathlib, pandas

from abc import ABC, abstractmethod
from copy import copy
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Type

from config import FILE_SETTING
from utility import get_df

from vnpy.event import Event, EventEngine
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.constant import (
    Offset,
    Direction,
    OrderType,
    Exchange,
)
from vnpy.trader.event import (
    EVENT_LOG,
    EVENT_TICK,
    EVENT_ORDER,
    EVENT_TRADE,
    EVENT_ACCOUNT,
    EVENT_POSITION,
    EVENT_CONTRACT,
)
from vnpy.trader.object import (
    LogData,
    TickData,
    TradeData,
    OrderData,
    AccountData,
    ContractData,
    PositionData,
    OrderRequest,
    SubscribeRequest,
    CancelRequest,
)

"""
####################### Change PositionData Struct ######################
"""
class MainEngine():
    """
    Only use for CTP like api.
    """
    def __init__(self, gateway_classes: Sequence[Type[BaseGateway]], settings: Dict[str, Dict[str, str]]) -> None:

        self.ticks: Dict[str, TickData] = {}
        self.orders: Dict[str, OrderData] = {}
        self.trades: Dict[str, TradeData] = {}
        self.accounts: Dict[str, AccountData] = {}
        self.contracts: Dict[str, ContractData] = {}
        self.positions: Dict[str, PositionData] = {}
        self.active_orders: Dict[str, OrderData] = {}

        self.event_engine = EventEngine()
        self._register_process_event()
        self.event_engine.start()

        self.engines: Dict[str, BaseEngine] = {}
        self._add_engines()

        self.gateways: Dict[str, BaseGateway] = {}
        self.gateway_classes: Dict[str, Type[BaseGateway]] = {}
        self.susbcribe_gateway_name: BaseGateway = None
        self._add_gateway_classes(gateway_classes)
        self._add_gateways(settings)
        self._connect_gateways(settings)

        self.log("Engine inited")

    def _add_engine(self, engine_class: Any) -> "BaseEngine":
        engine: BaseEngine = engine_class(self, self.event_engine)
        self.engines[engine.engine_name] = engine
        return engine

    def _add_engines(self) -> None:
        self._add_engine(DataEngine)
        self._add_engine(LogEngine)

    def _add_gateway(self, gateway_class_name: str, gateway_name: str) -> None:
        gateway_class = self.get_gateway_class(gateway_class_name)
        if gateway_class:
            gateway = gateway_class(self.event_engine, gateway_name)
            self.gateways[gateway.gateway_name] = gateway

    def _add_gateways(self, settings: Dict[str, Dict[str, str]]) -> None:
        for gateway_name, setting in settings.items():
            gateway_class_name = setting.get("Gateway")
            if gateway_class_name:
                self._add_gateway(gateway_class_name, gateway_name)
        self._add_subscribe_gateway_name()

    def _add_gateway_class(self, gateway_class: Type[BaseGateway]) -> None:
        self.gateway_classes[gateway_class.__name__] = gateway_class

    def _add_gateway_classes(self, gateway_classes: Sequence[Type[BaseGateway]]) -> None:
        for gateway_class in gateway_classes:
            self._add_gateway_class(gateway_class)

    def _add_subscribe_gateway_name(self) -> None:
        if self.susbcribe_gateway_name is None:
            self.susbcribe_gateway_name = self.get_all_gateways()[0].gateway_name

    def _connect(self, setting: Dict[str, str], gateway_name: str) -> None:
        gateway = self.get_gateway(gateway_name)
        if gateway:
            gateway.connect(setting)

    def _connect_gateways(self, settings: Dict[str, Dict[str, str]]) -> None:
        for gateway_name, setting in settings.items():
            self._connect(setting, gateway_name)
        
    def _subscribe(self, req: SubscribeRequest, gateway_name: str) -> None:
        gateway = self.get_gateway(gateway_name)
        if gateway:
            gateway.subscribe(req)

    def _send_order(self, req: OrderRequest, gateway_name: str) -> str:
        gateway = self.get_gateway(gateway_name)
        if gateway:
            vt_orderid = gateway.send_order(req)
            self.log(f"Send order {vt_orderid} {req.vt_symbol} {req.volume} {req.direction.value} {req.offset.value}", gateway_name)
            return vt_orderid
        return ""

    def _send_taker_order(self, vt_symbol: str, volume: float, direction: Direction, offset: Offset, gateway_name: str) -> List[str]:
        reqs: List[OrderRequest] = []
        vt_orderids: List[str] = []
        
        tick = self.get_tick(vt_symbol)
        contract = self.get_contract(vt_symbol)

        if tick is None or contract is None:
            vt_orderids.append("")
            self.log("Tick or contract data is none", gateway_name)
            return vt_orderids

        if direction == Direction.LONG:
            price = tick.ask_price_1 + contract.pricetick * 2
        else:
            price = tick.bid_price_1 - contract.pricetick * 2

        req = OrderRequest(
            symbol = contract.symbol,
            exchange = contract.exchange,
            price = price,
            volume = volume,
            direction = direction,
            offset = offset,
            type = OrderType.LIMIT
        )

        if offset == Offset.CLOSE:
            if direction == Direction.LONG:
                position = self.get_position(f"{gateway_name}.{contract.vt_symbol}.{Direction.SHORT.value}")
            else:
                position = self.get_position(f"{gateway_name}.{contract.vt_symbol}.{Direction.LONG.value}")
                
            if not position:
                vt_orderids.append("")
                self.log("Position data is none", gateway_name)
                return vt_orderids
            elif position.volume - position.frozen < volume:
                vt_orderids.append("")
                self.log("No enough close volume", gateway_name)
                return vt_orderids

            if contract.exchange in [Exchange.SHFE, Exchange.INE]:
                if not position.yd_volume:
                    req.offset = Offset.CLOSETODAY
                elif position.volume == position.yd_volume:
                    req.offset = Offset.CLOSEYESTERDAY
                else:
                    req.volume = position.yd_volume - position.frozen
                    req.offset = Offset.CLOSEYESTERDAY

                    req_td = copy(req)
                    req_td.volume = volume - req.volume
                    req_td.offset = Offset.CLOSETODAY
                    reqs.append(req_td)

        reqs.append(req)

        for req in reqs:
            vt_orderids.append(self._send_order(req, gateway_name))

        return vt_orderids

    def _cancel_order(self, req: CancelRequest, gateway_name:str) -> None:
        gateway = self.get_gateway(gateway_name)
        if gateway:
            gateway.cancel_order(req)
    
    def _process_tick_event(self, event: Event) -> None:
        tick: TickData = event.data
        self.ticks[tick.vt_symbol] = tick

    def _process_order_event(self, event: Event) -> None:
        order: OrderData = event.data
        self.orders[order.vt_orderid] = order

        if order.is_active():
            self.active_orders[order.vt_orderid] = order
        elif order.vt_orderid in self.active_orders:
            self.active_orders.pop(order.vt_orderid)

    def _process_trade_event(self, event: Event) -> None:
        trade: TradeData = event.data
        self.trades[trade.vt_tradeid] = trade
        self.log(f"Trade {trade.vt_symbol} {trade.volume} {trade.direction.value} {trade.offset.value}", trade.gateway_name)

    def _process_position_event(self, event: Event) -> None:
        position: PositionData = event.data
        self.positions[position.vt_positionid] = position

    def _process_contract_event(self, event: Event) -> None:
        contract: ContractData = event.data
        if self.get_contract(contract.vt_symbol) is None:
            self.contracts[contract.vt_symbol] = contract

    def _process_account_event(self, event: Event) -> None:
        account: AccountData = event.data
        self.accounts[account.gateway_name] = account

    def _register_process_event(self) -> None:
        self.event_engine.register(EVENT_TICK, self._process_tick_event)
        self.event_engine.register(EVENT_ORDER, self._process_order_event)
        self.event_engine.register(EVENT_TRADE, self._process_trade_event)
        self.event_engine.register(EVENT_ACCOUNT, self._process_account_event)
        self.event_engine.register(EVENT_CONTRACT, self._process_contract_event)
        self.event_engine.register(EVENT_POSITION, self._process_position_event)

    def get_gateway(self, gateway_name: str) -> Optional[BaseGateway]:
        return self.gateways.get(gateway_name)

    def get_gateway_class(self, gateway_class_name: str) -> Optional[Type[BaseGateway]]:
        return self.gateway_classes.get(gateway_class_name)

    def get_subscribe_gateway_name(self) -> Optional[BaseGateway]:
        return self.susbcribe_gateway_name

    def get_engine(self, engine_name: str) -> Optional["BaseEngine"]:
        return self.engines.get(engine_name)

    def get_tick(self, vt_symbol: str, use_df: bool = False) -> Optional[TickData]:
        return get_df(self.ticks.get(vt_symbol), use_df)

    def get_order(self, vt_orderid: str, use_df: bool = False) -> Optional[OrderData]:
        return get_df(self.orders.get(vt_orderid), use_df)

    def get_active_order(self, vt_orderid: str, use_df: bool = False) -> Optional[OrderData]:
        return get_df(self.active_orders.get(vt_orderid), use_df)

    def get_trade(self, vt_tradeid: str, use_df: bool = False) -> Optional[TradeData]:
        return get_df(self.trades.get(vt_tradeid), use_df)

    def get_position(self, vt_positionid: str, use_df: bool = False) -> Optional[PositionData]:
        return get_df(self.positions.get(vt_positionid), use_df)

    def get_contract(self, vt_symbol: str, use_df: bool = False) -> Optional[ContractData]:
        return get_df(self.contracts.get(vt_symbol), use_df)

    def get_account(self, gateway_name: str) -> Optional[AccountData]:
        return self.accounts.get(gateway_name)

    def get_all_gateways(self) -> List[BaseGateway]:
        return list(self.gateways.values())

    def get_all_gateway_names(self) -> List[str]:
        return list(self.gateways.keys())
        
    def get_all_gateway_classes(self) -> List[Type[BaseGateway]]:
        return list(self.gateway_classes.values())

    def get_all_gateway_class_names(self) -> List[str]:
        return list(self.gateway_classes.keys())

    def get_all_ticks(self, use_df: bool = False) -> List[TickData]:
        return get_df(list(self.ticks.values()), use_df)

    def get_all_orders(self, use_df: bool = False) -> List[OrderData]:
        return get_df(list(self.orders.values()), use_df)

    def get_all_active_orders(self, use_df: bool = False) -> List[OrderData]:
        return get_df(list(self.active_orders.values()), use_df)

    def get_all_trades(self, use_df: bool = False) -> List[TradeData]:
        return get_df(list(self.trades.values()), use_df)

    def get_all_positions(self, use_df: bool = False) -> List[PositionData]:
        return get_df(list(self.positions.values()), use_df)

    def get_all_contracts(self, use_df: bool = False) -> List[ContractData]:
        return get_df(list(self.contracts.values()), use_df)

    def get_all_accounts(self, use_df: bool = False) -> List[AccountData]:
        return get_df(list(self.accounts.values()), use_df) 

    def is_gateway_inited(self, gateway_name: str) -> bool:
        gateway = self.get_gateway(gateway_name)
        if gateway:
            return gateway.td_api.contract_inited
        return False

    def susbcribe(self, vt_symbols: List[str]) -> None:
        gateway_name = self.get_subscribe_gateway_name()
        if gateway_name:
            for vt_symbol in vt_symbols:
                contract = self.get_contract(vt_symbol)
                if contract:
                    req = SubscribeRequest(
                        symbol = contract.symbol,
                        exchange = contract.exchange
                    )
                    self._subscribe(req, gateway_name)
            self.log(f"Subscribe {vt_symbols}", gateway_name)

    def buy(self, vt_symbol: str, volume: float, gateway_name: str) -> List[str]:
        return self._send_taker_order(vt_symbol, volume, Direction.LONG, Offset.OPEN, gateway_name)
    
    def sell(self, vt_symbol: str, volume: float, gateway_name: str) -> List[str]:
        return self._send_taker_order(vt_symbol, volume, Direction.SHORT, Offset.CLOSE, gateway_name)

    def short(self, vt_symbol: str, volume: float, gateway_name: str) -> List[str]:
        return self._send_taker_order(vt_symbol, volume, Direction.SHORT, Offset.OPEN, gateway_name)

    def cover(self, vt_symbol: str, volume: float, gateway_name: str) -> List[str]:
        return self._send_taker_order(vt_symbol, volume, Direction.LONG, Offset.CLOSE, gateway_name)

    def cancel_active_order(self, vt_orderid: str) -> None:
        active_order = self.get_active_order(vt_orderid)
        if active_order:
            req = active_order.create_cancel_request()
            self._cancel_order(req, active_order.gateway_name)
            self.log(f"Cancel active order {active_order.vt_orderid} {active_order.vt_symbol} {active_order.volume - active_order.traded} {active_order.direction.value} {active_order.offset.value}")

    def log(self, msg: str, gateway_name: str = None, level: int = logging.INFO) -> None:
        log = LogData(gateway_name, msg, level)
        event = Event(EVENT_LOG, log)
        self.event_engine.put(event)

    def close(self) -> None:
        self.event_engine.stop()

        for engine in self.engines.values():
            engine.close()

        for gateway in self.gateways.values():
            gateway.close()


class BaseEngine(ABC):
    def __init__(self, main_engine: MainEngine, event_engine: EventEngine, engine_name: str) -> None:
        self.main_engine = main_engine
        self.event_engine = event_engine
        self.engine_name = engine_name

    @abstractmethod
    def close(self) -> None:
        pass


class DataEngine(BaseEngine):
    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, "DataEngine")

        self.datas: Dict[str, pandas.DataFrame]  = {}
        
        self.load_file_paths: Dict[str, str] = {}
        self.backup_file_paths: Dict[str, str] = {}

        self._add_load_dir_path()
        self._add_backup_dir_path()
        self._add_function()

    def _add_load_dir_path(self) -> None:
        self.load_dir_path = pathlib.Path(FILE_SETTING.get("ORDER_DIR_PATH"))
        if not self.load_dir_path.exists():
            self.load_dir_path.mkdir()

    def _add_backup_dir_path(self) -> None:
        self.backup_dir_path = pathlib.Path(FILE_SETTING.get("BACKUP_DIR_PATH"))
        if not self.backup_dir_path.exists():
            self.backup_dir_path.mkdir()

    def _add_function(self) -> None:
        self.main_engine.get_load_dir_path = self.get_load_dir_path
        self.main_engine.get_backup_dir_path = self.get_backup_dir_path

        self.main_engine.add_load_file_path = self.add_load_file_path
        self.main_engine.get_load_file_path = self.get_load_file_path
        self.main_engine.delete_load_file = self.delete_load_file

        self.main_engine.add_backup_file_path = self.add_backup_file_path
        self.main_engine.get_backup_file_path = self.get_backup_file_path
        self.main_engine.delete_backup_file = self.delete_backup_file

        self.main_engine.add_data = self.add_data
        self.main_engine.get_data = self.get_data
        self.main_engine.load_data = self.load_data
        self.main_engine.delete_data = self.delete_data
        self.main_engine.backup_data = self.backup_data

    def get_load_dir_path(self) -> pathlib.Path:
        return self.load_dir_path

    def get_backup_dir_path(self) -> pathlib.Path:
        return self.backup_dir_path
    
    def add_load_file_path(self, gateway_name: str, file_name: str) -> None:
        self.load_file_paths[gateway_name] = self.load_dir_path.joinpath(file_name)

    def get_load_file_path(self, gateway_name: str) -> pathlib.Path:
        return self.load_file_paths.get(gateway_name)

    def delete_load_file(self, gateway_name: str) -> None:
        file_path = self.get_load_file_path(gateway_name)
        if file_path.exists():
            file_path.unlink()

    def add_backup_file_path(self, gateway_name: str, file_name: str) -> None:
        self.backup_file_paths[gateway_name] = self.backup_dir_path.joinpath(file_name)
    
    def get_backup_file_path(self, gateway_name: str) -> pathlib.Path:
        return self.backup_file_paths.get(gateway_name)

    def delete_backup_file(self, gateway_name: str) -> None:
        file_path = self.get_backup_file_path(gateway_name)
        if file_path.exists():
            file_path.unlink()

    def add_data(self, gateway_name: str, data: pandas.DataFrame) -> None:
        self.datas[gateway_name] = data

    def get_data(self, gateway_name: str) -> Optional[pandas.DataFrame]:
        return self.datas.get(gateway_name)

    def load_data(self, gateway_name: str) -> pandas.DataFrame:
        file_path = self.get_backup_file_path(gateway_name)
        if not file_path.exists():
            file_path = self.get_load_file_path(gateway_name)

        data = pandas.read_csv(file_path)
        self.add_data(gateway_name, data)

        self.main_engine.log("Data loaded", gateway_name)
        return data

    def delete_data(self, gateway_name: str) -> None:
        self.datas.pop(gateway_name, None)

    def backup_data(self, gateway_name: str) -> None:
        data = self.get_data(gateway_name)
        file_path = self.get_backup_file_path(gateway_name)
        data.to_csv(file_path, index=False)

    def close(self) -> None:
        return super().close()


class LogEngine(BaseEngine):
    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        super().__init__(main_engine, event_engine, "LogEngine")

        self.logger = logging.getLogger("MainEngine")
        self.formatter = logging.Formatter("%(asctime)s  %(levelname)s: %(message)s")
        self.logger.setLevel(logging.INFO)
        
        self._add_log_dir_path()

        self._add_file_handler()
        self._add_console_handler()

        self._register_event()
    
    def _add_log_dir_path(self) -> None:
        self.log_dir_path = pathlib.Path(FILE_SETTING.get("LOG_DIR_PATH"))
        if not self.log_dir_path.exists():
            self.log_dir_path.mkdir()

    def _add_file_handler(self) -> None:
        file_name = f"{datetime.now().strftime('%Y%m%d')}.log"
        file_path = self.log_dir_path.joinpath(file_name)

        file_handler = logging.FileHandler(file_path, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(self.formatter)

        self.logger.addHandler(file_handler)

    def _add_console_handler(self) -> None:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(self.formatter)

        self.logger.addHandler(console_handler)

    def _process_log_event(self, event: Event) -> None:
        log: LogData = event.data
        if log.gateway_name:
            self.logger.log(log.level, f"{log.gateway_name} {log.msg}")
        else:
            self.logger.log(log.level, log.msg)

    def _register_event(self) -> None:
        self.event_engine.register(EVENT_LOG, self._process_log_event)

    def close(self) -> None:
        return super().close()