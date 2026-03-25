from statemachine import State, StateMachine

class TruckWorkflowStateMachine(StateMachine):
    off_duty = State('Off Duty', initial=True)
    on_duty = State('On Duty')
    resting = State('Resting')

    start_work = off_duty.to(on_duty)
    end_work = on_duty.to(off_duty)
    rest = on_duty.to(resting)
    resume = resting.to(on_duty)
