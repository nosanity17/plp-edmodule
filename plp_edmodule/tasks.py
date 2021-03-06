# coding: utf-8

from django.utils import timezone
from celery.schedules import crontab
from celery.task import periodic_task
from plp.models import CourseSession
from .notifications import EdmoduleCourseStartsEmails, EdmoduleCourseEnrollEndsEmails


@periodic_task(run_every=crontab(minute=0, hour=0))
def send_notification_module_course_starts():
    """
    периодическая проверка курсов, которые стартуют и отправка сообщений пользователям,
    которые не записаны на курс, который входит в модуль, на который они записаны
    """
    now = timezone.now()
    qs = CourseSession.objects.filter(datetime_starts__range=(
        timezone.make_aware(timezone.datetime.combine(now, timezone.datetime.min.time())),
        timezone.make_aware(timezone.datetime.combine(now, timezone.datetime.max.time()))
    ))
    for cs in qs:
        emails = EdmoduleCourseStartsEmails(cs)
        emails.send()


@periodic_task(run_every=crontab(minute=0, hour=0))
def send_notification_module_course_enroll_ends():
    """
    периодическая проверка курсов, запись на которые заканчивается и отправка сообщений пользователям,
    которые не записаны на курс, который входит в модуль, на который они записаны
    """
    now = timezone.now()
    td = timezone.timedelta(days=7)
    qs = CourseSession.objects.filter(datetime_end_enroll__range=(
        timezone.make_aware(timezone.datetime.combine(now+td, timezone.datetime.min.time())),
        timezone.make_aware(timezone.datetime.combine(now+td, timezone.datetime.max.time()))
    ))
    for cs in qs:
        emails = EdmoduleCourseEnrollEndsEmails(cs)
        emails.send()
