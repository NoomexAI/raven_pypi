class Status:
    def __init__(self, initial_status= None, on_change= None):
        self._status = initial_status
        self._listeners = {}
        self.on_change = on_change

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        if not self.status == value:
            self._status = value

            if self.on_change:
                self.on_change(status=self._status)

            listeners = self._listeners.get(self.status, [])[:]
            for callback, run_once in listeners:
                if callback:
                    callback(status=self.status)

                    if run_once:
                        self._listeners[self.status].remove((callback, run_once))

    
    def on_status(self, target_status, callback, run_once=False):
        if not target_status in self._listeners:
            self._listeners[target_status] = []
        self._listeners[target_status].append((callback, run_once))
