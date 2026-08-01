class PCF8574:
    def __init__(self, i2c, address=0x20):
        self.i2c = i2c
        self.address = address
        self._port = self.i2c.readfrom(self.address, 1)[0]

    def pin(self, pin, value=None):
        if value is None:
            return (self._port >> pin) & 1
        if value:
            self._port |= (1 << pin)
        else:
            self._port &= ~(1 << pin)
        self.i2c.writeto(self.address, bytes([self._port]))
        
    def port(self, value=None):
        if value is None:
            return self._port
        self._port = value
        self.i2c.writeto(self.address, bytes([self._port]))
