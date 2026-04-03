from statemachine import State, StateMachine

class OrderStateMachine(StateMachine):
    created = State('Created', initial=True)
    in_market = State('In Market')
    assigned = State('Assigned')
    completed = State('Completed', final=True)
    expired = State('Expired', final=True)
    cancelled = State('Cancelled', final=True)

    enter_market = created.to(in_market)
    assign = in_market.to(assigned)
    complete = assigned.to(completed)
    expire = (created | in_market).to(expired)
    cancel = (created | in_market | assigned).to(cancelled)
