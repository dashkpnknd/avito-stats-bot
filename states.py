from aiogram.fsm.state import State, StatesGroup

class AddStore(StatesGroup):
    waiting_for_name = State()
    waiting_for_chat_id = State()
    waiting_for_client_id = State()
    waiting_for_client_secret = State()
    waiting_for_client_mention = State()


class EditStore(StatesGroup):
    waiting_for_client_mention = State()
