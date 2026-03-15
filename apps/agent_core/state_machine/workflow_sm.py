from statemachine import State, StateMachine

class WorkflowStateMachine(StateMachine):
    ''' '''

    dormant = State('dormant', initial=True)
    offline = State('offline')
    online = State('online')

    register = dormant.to(offline)
    deregister = offline.to(dormant)
    login = offline.to(online)
    logout = offline.from_(online)
