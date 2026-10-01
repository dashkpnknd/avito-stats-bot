import asyncio
import logging

from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from zoneinfo import ZoneInfo

import config
import database
from handlers import router
from scheduler import check_low_balances_job, scheduled_report_job


async def main() -> None:
    config.validate_config()
    await database.init_db()
    bot = Bot(token=config.BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    scheduler = AsyncIOScheduler(timezone=ZoneInfo(config.TIMEZONE))
    scheduler.add_job(
        scheduled_report_job,
        trigger="cron",
        id="daily_reports",
        hour=config.DAILY_HOUR,
        minute=config.DAILY_MINUTE,
        kwargs={"bot": bot, "report_type": "daily"},
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=1800,
    )
    scheduler.add_job(
        scheduled_report_job,
        trigger="cron",
        id="weekly_reports",
        day_of_week="mon",
        hour=config.WEEKLY_HOUR,
        minute=config.WEEKLY_MINUTE,
        kwargs={"bot": bot, "report_type": "weekly"},
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=7200,
    )
    scheduler.add_job(
        check_low_balances_job,
        trigger="interval",
        id="low_balance_checks",
        minutes=config.LOW_BALANCE_CHECK_MINUTES,
        kwargs={"bot": bot},
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        scheduled_report_job,
        trigger="cron",
        id="monthly_reports",
        # The cron expression wakes on the possible final days. The job
        # itself confirms the actual final calendar day (including February).
        day="28-31",
        hour=config.MONTHLY_HOUR,
        minute=config.MONTHLY_MINUTE,
        kwargs={"bot": bot, "report_type": "monthly"},
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=7200,
    )
    scheduler.start()
    logging.info("Бот запущен; расписания: %s", scheduler.get_jobs())

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен")
