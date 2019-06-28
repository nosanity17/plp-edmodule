# coding: utf-8

import random
from collections import defaultdict
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core import validators
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
from django.utils import timezone
from django.utils.functional import cached_property, SimpleLazyObject
from django.utils.translation import ugettext_lazy as _
from jsonfield import JSONField
from sortedm2m.fields import SortedManyToManyField
from imagekit.models import ImageSpecField
from imagekit.processors import Resize
from datetime import datetime
from decimal import Decimal
from plp.models import Course, User, SessionEnrollmentType, Participant, CourseSession, EnrollmentReason
from plp_extension.apps.module_extension.models import DEFAULT_COVER_SIZE
from plp_extension.apps.course_extension.models import CourseExtendedParameters
from .signals import edmodule_enrolled, edmodule_enrolled_handler, edmodule_payed, edmodule_payed_handler, \
    edmodule_unenrolled, edmodule_unenrolled_handler

HIDDEN = 'hidden'
DIRECT = 'direct'
PUBLISHED = 'published'

ICON_THUMB_SIZE = (
    getattr(settings, 'BENEFIT_ICON_SIZE', (100, 100))[0],
    getattr(settings, 'BENEFIT_ICON_SIZE', (100, 100))[1]
)


class EducationalModule(models.Model):
    STATUSES = (
        (HIDDEN, _('Скрыт')),
        (DIRECT, _('Доступ по ссылке')),
        (PUBLISHED, _('Опубликован')),
    )
    code = models.SlugField(verbose_name=_('Код'), unique=True)
    title = models.CharField(verbose_name=_('Название'), max_length=200)
    status = models.CharField(_('Статус'), max_length=16, choices=STATUSES, default='hidden')
    courses = SortedManyToManyField(Course, verbose_name=_('Курсы'), related_name='education_modules')
    cover = models.ImageField(_('Обложка EM'), upload_to='edmodule_cover', blank=True,
        help_text=_('Минимум {0}*{1}, картинки большего размера будут сжаты до этого размера').format(
            *getattr(settings, 'EDMODULE_COVER_IMAGE_SIZE', DEFAULT_COVER_SIZE)
    ))
    about = models.TextField(verbose_name=_('Описание'), blank=False)
    price = models.IntegerField(verbose_name=_('Стоимость'), blank=True, null=True)
    discount = models.IntegerField(verbose_name=_('Скидка'), blank=True, default=0, validators=[
        validators.MinValueValidator(0),
        validators.MaxValueValidator(100)
    ])
    vacancies = models.TextField(verbose_name=_('Вакансии'), blank=True, default='', help_text=_('HTML блок'))
    subtitle = models.TextField(verbose_name=_('Подзаголовок'), blank=True, default='',
                                help_text=_('от 1 до 3 элементов, каждый с новой строки'))
    sum_ratings = models.PositiveIntegerField(verbose_name=_('Сумма оценок'), default=0)
    count_ratings = models.PositiveIntegerField(verbose_name=_('Количество оценок'), default=0)

    class Meta:
        verbose_name = _('Образовательный модуль')
        verbose_name_plural = _('Образовательные модули')

    def __str__(self):
        return '%s - %s' % (self.code, ', '.join(self.courses.values_list('slug', flat=True)))

    @cached_property
    def duration(self):
        """
        сумма длительностей курсов (в неделях)
        """
        duration = 0
        for c, s in self.courses_with_closest_sessions:
            d = s.get_duration() if s else c.duration
            if not d:
                return 0
            duration += d
        return duration

    @cached_property
    def whole_work(self):
        work = 0
        for c, s in self.courses_with_closest_sessions:
            if s:
                min_workload, max_workload = s.get_workload_from(), s.get_workload_to
                if min_workload is not None and max_workload is not None:
                    w = (s.get_duration() or 0) * int((s.get_workload_from() + s.get_workload_to()) / 2)
                else:
                    w = 0
            else:
                if c.workload_from is not None and c.workload_to is not None:
                    w = (c.duration or 0) * int((c.workload_from + c.workload_to) / 2)
                else:
                    w = 0
            if not w:
                return 0
            work += w
        return work

    @property
    def workload(self):
        work = self.whole_work
        duration = self.duration
        if self.duration:
            return int(round(float(work) / duration, 0))
        return 0

    @property
    def instructors(self):
        """
        объединение множества преподавателей всех курсов модуля
        упорядочивание по частоте вхождения в сессии, на которые мы записываем пользователя
        """
        d = {}
        for c in self.courses.all():
            if c.next_session:
                for i in c.next_session.get_instructors():
                    d[i] = d.get(i, 0) + 1
            else:
                for i in c.instructor.all():
                    d[i] = d.get(i, 0) + 1
        result = sorted(list(d.items()), key=lambda x: x[1], reverse=True)
        return [i[0] for i in result]

    @property
    def categories(self):
        return self._get_sorted('categories')

    def get_authors(self):
        return self._get_sorted('authors')

    def get_partners(self):
        return self._get_sorted('partners')

    def get_authors_and_partners(self):
        result = []
        for i in self.get_authors() + self.get_partners():
            if not i in result:
                result.append(i)
        return result

    def _get_sorted(self, attr):
        """
        Возвращает список элементов attr отсортированный по количеству курсов,
        в которых этот attr встречается. Используется, например, для списка категорий
        модуля, которые отстортированы по количеству курсов, в которых они встречаются
        """
        d = {}
        for c in self.courses_extended.prefetch_related(attr):
            for item in getattr(c, attr).all():
                d[item] = d.get(item, 0) + 1
        result = sorted(list(d.items()), key=lambda x: x[1], reverse=True)
        return [i[0] for i in result]

    def get_schedule(self):
        """
        список тем
        """
        schedule = []
        all_courses = self.courses.values_list('id', flat=True)
        for c in self.courses_extended.prefetch_related('course'):
            if c.course.id not in all_courses:
                schedule.append({'course': {'title': c.course.title},
                                 'schedule': ''})
            else:
                schedule.append({'course': {'title': c.course.title},
                                 'schedule': c.themes})
        return schedule

    def get_rating(self):
        if self.count_ratings:
            return round(float(self.sum_ratings) / self.count_ratings, 2)
        return 0

    def get_related(self):
        """
        получение похожих курсов и специализаций (от 0 до 2)
        """
        categories = self.categories
        if not categories:
            return []
        modules = EducationalModule.objects.exclude(id=self.id).filter(
            courses__extended_params__categories__in=categories,status='published').distinct()
        courses = EdmoduleCourse.objects.exclude(id__in=self.courses.values_list('id', flat=True)).filter(
            extended_params__categories__in=categories,status='published').distinct()
        related = []
        if modules:
            related.append({'type': 'em', 'item': random.sample(list(modules), 1)[0]})
        if courses:
            sample = random.sample(list(courses), min(len(courses), 2))
            for i in range(2 - len(related)):
                try:
                    related.append({'type': 'course', 'item': sample[i]})
                except IndexError:
                    pass
        return related

    def get_sessions(self):
        """
        хелпер для выбора сессий
        """
        return [i.next_session for i in self.courses.all()]

    @cached_property
    def courses_extended(self):
        """
        CourseExtendedParameters всех курсов модуля
        """
        return CourseExtendedParameters.objects.filter(course__id__in=self.courses.values_list('id', flat=True))

    def get_module_profit(self):
        """ для блока "что я получу в итоге" """
        data = []
        for c in self.courses_extended:
            if c.profit:
                data.extend(c.profit.splitlines())
        data = [i.strip() for i in data if i.strip()]
        return list(set(data))

    def get_requirements(self):
        try:
            s = self.extended_params.requirements or ''
            return [i.strip() for i in s.splitlines() if i.strip()]
        except:
            pass

    def get_price_list(self, for_user=None):
        """
        :return: {
            'courses': [(курс(Course), цена(int), ...],
            'price': цена без скидок (int),
            'whole_price': цена со скидкой (float),
            'discount': скидка (int)
        }
        """
        courses = self.courses.all()
        # берем цену ближайшей сессии, на которую можно записаться, или предыдущей
        session_for_course = {}
        now = timezone.now()
        course_paid = []
        if for_user and for_user.is_authenticated:
            # если пользователь платил за какую-то сессию курса и успешно ее окончил или она
            # еще не завершилась, цена курса для него 0
            reasons = EnrollmentReason.objects.filter(
                participant__user=for_user,
                session_enrollment_type__mode='verified'
            ).select_related('participant', 'participant__session')
            payment_for_course = defaultdict(list)
            for r in reasons:
                payment_for_course[r.participant.session.course_id].append(r)
            for course_id, payments in payment_for_course.items():
                should_pay = True
                for r in payments:
                    if r.participant.is_graduate:
                        should_pay = False
                        break
                    if r.participant.session.datetime_ends and r.participant.session.datetime_ends > now:
                        should_pay = False
                        break
                if not should_pay:
                    course_paid.append(course_id)
        exclude = {'id__in': course_paid}
        sessions = CourseSession.objects.filter(
            course__in=courses.exclude(**exclude),
            datetime_end_enroll__isnull=False,
            datetime_start_enroll__lt=now
        ).exclude(**exclude).order_by('-datetime_end_enroll')
        courses_with_sessions = defaultdict(list)
        for s in sessions:
            courses_with_sessions[s.course_id].append(s)
        for c, course_sessions in courses_with_sessions.items():
            if course_sessions:
                session_for_course[c] = course_sessions[0]
        types = dict([(i.session.id, i.price) for i in
                      SessionEnrollmentType.objects.filter(session__in=list(session_for_course.values()), mode='verified')])
        result = {'courses': []}
        for c in courses:
            s = session_for_course.get(c.id)
            if s:
                result['courses'].append((c, types.get(s.id, 0)))
            else:
                result['courses'].append((c, 0))
        price = sum([i[1] for i in result['courses']])
        whole_price = price * (1 - self.discount / 100.)
        result.update({
            'price': price,
            'whole_price': whole_price,
            'discount': self.discount
        })
        return result

    def get_start_date(self):
        """
        дата старта первого курса модуля
        """
        c = self.courses.first()
        if c and c.next_session:
            return c.next_session.datetime_starts

    def course_status_params(self):
        from .utils import get_status_dict
        c = self.get_closest_course_with_session()
        if c:
            return get_status_dict(c[1])
        return {}

    @property
    def count_courses(self):
        return self.courses.count()

    @cached_property
    def courses_with_closest_sessions(self):
        from .utils import choose_closest_session
        courses = self.courses.exclude(extended_params__is_project=True)
        return [(c, choose_closest_session(c)) for c in courses]

    def get_closest_course_with_session(self):
        """
        первый курс, не являющийся проектом, и соответствующая сессия модуля
        """
        for c in self.courses.filter(extended_params__is_project=False):
            session = c.next_session
            if session and session.get_verified_mode_enrollment_type():
                return c, session

    def may_enroll(self):
        """
        Проверка того, что пользователь может записаться на модуль
        :return: bool
        """
        courses = self.courses_with_closest_sessions
        return all(i[1] and i[1].allow_enrollments() for i in courses)

    def may_enroll_on_project(self, user):
        """
        Проверка того, что пользователь может записаться на проект
        :param user: User
        :return: bool
        """
        if not user.is_authenticated:
            return False
        if not EducationalModuleEnrollment.objects.filter(user=user, module=self, is_active=True).exists():
            return False
        courses = self.courses.filter(extended_params__is_project=False).values_list('id', flat=True)
        passed = {i: False for i in courses}
        participants = Participant.objects.filter(session__course__id__in=courses, user=user).values_list(
            'session__course__id', 'is_graduate')
        for course_id, is_graduate in participants:
            if is_graduate:
                passed[course_id] = True
        return all(i for i in list(passed.values()))

    def get_available_enrollment_types(self, mode=None, exclude_expired=True, active=True):
        """ Возвращает доступные варианты EducationalModuleEnrollmentType для текущего модуля """
        qs = EducationalModuleEnrollmentType.objects.filter(module=self)
        if active:
            qs = qs.filter(active=True)
        if mode:
            qs = qs.filter(mode=mode)
        if exclude_expired and mode == 'verified':
            qs = qs.exclude(buy_expiration__lt=timezone.now()).filter(
                models.Q(buy_start__isnull=True) | models.Q(buy_start__lt=timezone.now())
            )
        return qs

    def get_verified_mode_enrollment_type(self):
        """
        Метод аналогичный CourseSession
        """
        return self.get_available_enrollment_types(mode='verified').first()

    def get_enrollment_reason_for_user(self, user):
        """
        queryset EducationalModuleEnrollmentReason для пользователя, первый элемент - полностью оплаченный,
        если такой есть
        """
        if user.is_authenticated:
            return EducationalModuleEnrollmentReason.objects.filter(
                enrollment__user=user,
                enrollment__module=self,
            ).order_by('-full_paid').first()

    def get_first_session_to_buy(self, user):
        """
        Сессия первого курса, который пользователь может купить.
        Возвращает (сессия, цена) или None
        """
        auth = user.is_authenticated if user else None
        for course in self.courses.exclude(extended_params__is_project=True):
            session = course.next_session
            if session:
                enr_type = session.get_verified_mode_enrollment_type()
                if enr_type and auth:
                    if not enr_type.is_user_enrolled(user):
                        return session, enr_type.price
                elif enr_type:
                    return session, enr_type.price


class EducationalModuleEnrollment(models.Model):
    user = models.ForeignKey(User, verbose_name=_('Пользователь'), on_delete=models.CASCADE)
    module = models.ForeignKey(EducationalModule, verbose_name=_('Образовательный модуль'), on_delete=models.CASCADE)
    is_paid = models.BooleanField(verbose_name=_('Прохождение оплачено'), default=False)
    is_graduated = models.BooleanField(verbose_name=_('Прохождение завершено'), default=False)
    is_active = models.BooleanField(default=False)
    _ctime = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Запись на модуль')
        verbose_name_plural = _('Записи на модуль')
        unique_together = ('user', 'module')

    def __str__(self):
        return '%s - %s' % (self.user, self.module)


class EducationalModuleProgress(models.Model):
    enrollment = models.OneToOneField(EducationalModuleEnrollment, verbose_name=_('Запись на модуль'),
                                      related_name='progress', on_delete=models.CASCADE)
    progress = JSONField(verbose_name=_('Прогресс'), null=True)
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_('Время последнего обращения к edx'))

    class Meta:
        verbose_name = _('Прогресс по модулю')
        verbose_name_plural = _('Прогресс по модулям')


class EducationalModuleUnsubscribe(models.Model):
    user = models.ForeignKey(User, verbose_name=_('Пользователь'), on_delete=models.CASCADE)
    module = models.ForeignKey(EducationalModule, verbose_name=_('Образовательный модуль'), on_delete=models.CASCADE)

    class Meta:
        verbose_name = _('Отписка от рассылок образовательного модуля')
        verbose_name_plural = _('Отписки от рассылок образовательного модуля')
        unique_together = ('user', 'module')


class EducationalModuleEnrollmentType(models.Model):
    EDX_MODES = (
        ('audit', 'audit'),
        ('honor', 'honor'),
        ('verified', 'verified')
    )

    module = models.ForeignKey(EducationalModule, verbose_name=_('Образовательный модуль'), on_delete=models.CASCADE)
    active = models.BooleanField(_('Активен'), default=True)
    mode = models.CharField(_('Тип'), max_length=32, choices=EDX_MODES, blank=True, help_text=_('course mode в edx'))
    buy_start = models.DateTimeField(_('Начало приема оплаты'), null=True, blank=True)
    buy_expiration = models.DateField(_('Крайняя дата оплаты'), null=True, blank=True)
    price = models.PositiveIntegerField(_('Стоимость'), default=0)
    about = models.TextField(_('Краткое описание'), blank=True)
    description = models.TextField(_('Описание'), blank=True)

    class Meta:
        verbose_name = _('Вариант прохождения модуля')
        verbose_name_plural = _('Варианты прохождения модуля')
        unique_together = (("module", "mode"),)

    def __str__(self):
        return '%s - %s - %s' % (self.module, self.mode, self.price)


class EducationalModuleEnrollmentReason(models.Model):
    class PAYMENT_TYPE:
        MANUAL = 'manual'
        YAMONEY = 'yamoney'
        OTHER = 'other'
        CHOICES = [(v, v) for v in (MANUAL, YAMONEY, OTHER)]

    CHOICES = [(None, '')] + PAYMENT_TYPE.CHOICES
    enrollment = models.ForeignKey(EducationalModuleEnrollment, verbose_name=_('Запись на модуль'),
                                   related_name='enrollment_reason', on_delete=models.CASCADE)
    module_enrollment_type = models.ForeignKey(EducationalModuleEnrollmentType,
                                               verbose_name=_('Вариант прохождения модуля'), on_delete=models.CASCADE)
    payment_type = models.CharField(max_length=16, null=True, default=None, choices=CHOICES,
                                    verbose_name=_('Способ платежа'))
    payment_order_id = models.CharField(max_length=64, null=True, blank=True,
                                        help_text=_('Номер договора (для яндекс-кассы - поле order_number)'),
                                        verbose_name=_('Номер договора'))
    payment_descriptions = models.TextField(null=True, blank=True, help_text=_('Комментарий к платежу'),
                                            verbose_name=_('Описание платежа'))
    full_paid = models.BooleanField(verbose_name=_('Специализация оплачена полностью'), default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('Причина записи')
        verbose_name_plural = _('Причины записи')


class Benefit(models.Model):
    title = models.CharField(max_length=160, verbose_name=_('Название'))
    description = models.TextField(verbose_name=_('Описание'), blank=True, default='',
                                   validators=[validators.MaxLengthValidator(400)])
    icon = models.ImageField(verbose_name=_('Иконка'), upload_to='benefit_icons',
                             help_text=_('png, размер файла не более 1 мб, разрешение не более 1000*1000'))
    icon_thumbnail = ImageSpecField(source='icon', processors=[Resize(*ICON_THUMB_SIZE)])

    class Meta:
        verbose_name = _('Выгода')
        verbose_name_plural = _('Выгоды')

    def __str__(self):
        return self.title


class BenefitLink(models.Model):
    limit_models = models.Q(app_label='plp_edmodule', model='educationalmodule') | \
                   models.Q(app_label='plp', model='course')
    benefit = models.ForeignKey('Benefit', verbose_name=_('Выгода'), related_name='benefit_links', on_delete=models.CASCADE)
    content_type = models.ForeignKey(ContentType, limit_choices_to=limit_models,
                                     verbose_name=_('Тип объекта, к которому выгода'), on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField(verbose_name=_('Объект, к которому выгода'))
    content_object = GenericForeignKey('content_type', 'object_id')

    @staticmethod
    def get_benefits_for_object(obj):
        ctype = ContentType.objects.get_for_model(obj)
        return BenefitLink.objects.filter(
            content_type=ctype,
            object_id=obj.id
        ).select_related('benefit')


class CoursePromotion(models.Model):
    limit_models = models.Q(app_label='plp_edmodule', model='educationalmodule') | \
                   models.Q(app_label='plp', model='course')
    content_type = models.ForeignKey(ContentType, limit_choices_to=limit_models,
                                     verbose_name=_('Тип объекта'), on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField(verbose_name=_('Id объекта'))
    content_object = GenericForeignKey('content_type', 'object_id')
    content_object.short_description = _('Объект')
    sort = models.SmallIntegerField(verbose_name=_('Приоритет'))

    class Meta:
        verbose_name = _('Порядок курсов и специализаций на главной')
        verbose_name_plural = _('Порядок курсов и специализаций на главной')
        ordering = ['sort']

    def __str__(self):
        return '%s - %s' % (self.sort, self.content_object)

class PromoCode(models.Model):

    PRODUCTS = (
        ('course', _('Курс')),
        ('edmodule', _('Специализация')),
    )

    class Meta:
        verbose_name = _('Промокод')
        verbose_name_plural = _('Промокоды')
 
    code = models.CharField(_('Промокод'), max_length=6, blank=True, null=False)
    product_type = models.CharField(_('Тип продукта'), max_length=10, choices=PRODUCTS, default='course', blank=False, null=False)
    course = models.ForeignKey(Course, verbose_name=_('Курс'), blank=True, null=True, on_delete=models.CASCADE)
    edmodule = models.ForeignKey(EducationalModule, related_name='edmodule', verbose_name=_('Специализация'),
                                 blank=True, null=True, on_delete=models.CASCADE)
    active_till = models.DateField(_('Актуален до даты'), blank=False, null=False)
    max_usage = models.PositiveSmallIntegerField(_('Количество возможных оплат'), blank=False, null=False)
    used = models.PositiveSmallIntegerField(_('Был использован'), null=False)
    use_with_others = models.BooleanField(_('Применяется с другими скидками'), default=True)
    discount_percent = models.DecimalField(_('Процент скидки'), max_digits=5, decimal_places=2, blank=True, null=True)
    discount_price = models.DecimalField(_('Новая стоимость курса'), max_digits=8, decimal_places=2, blank=True, null=True)

    def __str__(self):
        product_name = self.edmodule.title if self.product_type == 'edmodule' else self.course.title
        discount = "%0.0f" % (self.discount_percent) + '%' if self.discount_percent else "%0.2f" % (self.discount_price) + ' руб'      
        return "{0} - {1} - {2}".format(self.code, product_name, discount)

    def validate(self, product_id, product_type):
        """ Возвращает словарь со статусом валидации (валиден = 0, есть ошибки = 1), 
            а также сообщение, содержащие суть ошибки """

        msg = 'данному курсу' if product_type == self.PRODUCTS[0][0] else 'данной специализации'
        if self.product_type == self.PRODUCTS[1][0] and not self.product_type == product_type and not self.edmodule.id == product_id:
            return {
                'status': 1,
                'message': str(_('Промокод не принадлежит ' + msg))
            }
        elif self.product_type == self.PRODUCTS[0][0] and not self.product_type == product_type and not self.course.id == product_id:
            return {
                'status': 1,
                'message': str(_('Промокод не принадлежит ' + msg))
            }

        if self.used >= self.max_usage:
            return {
                'status': 1,
                'message': str(_('Промокод уже был использован'))
            }

        
        if datetime.now().date() > self.active_till:
            return {
                'status': 1,
                'message': str(_('Срок действия промокода истек'))
            }

        return {
            'status': 0,
            'message': str(_('Промокод действителен'))
        }

    def calculate(self, product_id=None, only_first_course=None, session_id=None):
        """ Производит расчет по переданным параметрам и, в соответствие, с логикой задачи OP-614 """
            
        if self.discount_price:
            return {
                'status': 0,
                'new_price': self.discount_price
            }   

        if self.product_type == self.PRODUCTS[1][0]:
            try:
                edmodule = EducationalModule.objects.get(id=product_id)
            except ObjectDoesNotExist:
                return {
                    'status': 1,
                    'message': str(_('Не удалось найти специализацию'))
                }

            price = edmodule.get_price_list()

            if only_first_course == True:
                first_course_price = edmodule.get_first_session_to_buy(None)[1]
                price['price'] = first_course_price
                price['whole_price'] = first_course_price * (1 - price['discount'] / 100.)
            
            if self.use_with_others:
                full_discount = self.discount_percent + Decimal(price['discount'])
                new_price = Decimal(price['price']) * (1 - full_discount / 100)
                return {
                    'status': 0,
                    'new_price': new_price.quantize(Decimal('.00'))
                }    
            else:
                if Decimal(price['discount']) > self.discount_percent:
                    return {
                        'status': 0,
                        'new_price': Decimal(price['whole_price']).quantize(Decimal('.00'))
                    }  
                else:
                    new_price = Decimal(price['price']) * (1 - self.discount_percent / 100)
                    return {
                        'status': 0,
                        'new_price': new_price.quantize(Decimal('.00'))
                    }

        if self.product_type == self.PRODUCTS[0][0]:
            try:
                session = CourseSession.objects.get(id=session_id)
            except ObjectDoesNotExist:
                return {
                    'status': 1,
                    'message': str(_('Не удалось найти курс'))
                }

            verified = session.get_verified_mode_enrollment_type()
            new_price = Decimal(verified.price) * (1 - self.discount_percent / 100)
            
            return {
                'status': 0,
                'new_price': new_price.quantize(Decimal('.00'))
            }  


def _string_splitter(obj, attr):
    try:
        s = getattr(obj, attr)
        if s and isinstance(s, str):
            return [i.strip() for i in s.splitlines() if i.strip()]
    except AttributeError:
        pass
    return []


edmodule_course_additional_fields = SimpleLazyObject(
    lambda: [f.name for f in CourseExtendedParameters._meta.fields if not f.auto_created and f.editable])


class EdmoduleCourse(Course):
    class Meta:
        proxy = True

    def __getattribute__(self, item):
        if item in edmodule_course_additional_fields:
            return getattr(self.extended_params, item)
        return super().__getattribute__(item)

    def get_next_session(self):
        from plp_edmodule.utils import choose_closest_session
        return choose_closest_session(self)

    def course_status_params(self):
        from plp_edmodule.utils import get_status_dict
        session = self.get_next_session()
        if not session:
            return self.course_status()
        return get_status_dict(self.get_next_session())

    def get_requirements(self):
        return _string_splitter(self, 'requirements')

    def get_profit(self):
        return _string_splitter(self, 'profit')

    def get_documents(self):
        return _string_splitter(self, 'documents')

    def get_authors_and_partners(self):
        try:
            extended = self.extended_params
            result = []
            for i in list(extended.authors.all()) + list(extended.partners.all()):
                if i not in result:
                    result.append(i)
            return result
        except CourseExtendedParameters.DoesNotExist:
            return []

    def get_competencies(self):
        return []

    def get_course_format_list(self):
        return _string_splitter(self, 'course_format')


edmodule_enrolled.connect(edmodule_enrolled_handler, sender=EducationalModuleEnrollment)
edmodule_unenrolled.connect(edmodule_unenrolled_handler, sender=EducationalModuleEnrollment)
edmodule_payed.connect(edmodule_payed_handler, sender=EducationalModuleEnrollmentReason)
