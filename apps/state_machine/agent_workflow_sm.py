from statemachine import State, StateMachine


class UserStateMachine(StateMachine):
    ''' '''

    dormant = State('dormant', initial=True)
    # activation_requested = State('activation_requested')
    # activation_sent = State('activation_sent')
    # activation_failed = State('activation_failed')
    active = State('active')

    register = dormant.to(active)
    deregister = active.to(dormant)

class WorkflowStateMachine(StateMachine):
    ''' '''

    dormant = State('dormant', initial=True)
    offline = State('offline')
    online = State('online')

    register = dormant.to(offline)
    deregister = offline.to(dormant)
    login = offline.to(online)
    logout = offline.from_(online)



