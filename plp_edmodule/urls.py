# coding: utf-8

from django.conf.urls import url
from . import views


urlpatterns = [
    url(r'^$', views.Index.as_view(), name='frontpage'),
    # не используется, запись после оплаты через tp_payments
    # url(r'^edmodule-enroll/?$', views.edmodule_enroll, name='edmodule-enroll'),
    url(r'^edmodule/(?P<code>[-\w]+)/?$', views.module_page, name='edmodule-page'),
    url(r'^get-honor-text/?$', views.get_honor_text, name='get-honor-text'),
    url(r'^course/filter/?$', views.edmodule_filter_view, name='edmodule-filter'),
    url(r'^catalog/?$', views.edmodule_catalog_view, name='edmodule-catalog'),
    # url(r'^catalog/(?P<category>[-\w]+)/?$', views.edmodule_catalog_view, name='edmodule-catalog'),
    url(r'^org/(?P<code>[-\w]+)/?$', views.organization_view, name='edmodule-organisation'),
    url(r'^course/(?P<uni_slug>[-\w]*)/(?P<slug>[-\w]*)/$', views.CoursePage.as_view(), name='course_details'),
    url(r'^my/$', views.Cabinet.as_view(), name='my'),
]
