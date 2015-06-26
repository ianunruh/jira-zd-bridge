class Strategy(object):
    def get_escalation_contact(self):
        pass

    def post_escalation(self):
        pass

class SimpleStrategy(Strategy):
    def __init__(self, assignee):
        self.assignee = assignee

    def get_escalation_contact(self):
        return self.assignee
