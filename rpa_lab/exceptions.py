"""The two exception families of the REFramework.

BusinessRuleException  -> the data is wrong (unknown supplier, totals do not add up...). Retrying will not
                          help: the item is closed as a *Business* failure and routed to a human.
ApplicationException   -> the robot or a target system misbehaved (timeout, 503, crash). The item is closed
                          as an *Application* failure and Orchestrator re-queues it automatically.
"""


class BusinessRuleException(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ApplicationException(Exception):
    pass
