from django.contrib import admin

from .models import Group, GroupMembership, Role, User

# Register your models here.
admin.site.register(User)
admin.site.register(Group)
admin.site.register(Role)
admin.site.register(GroupMembership)
