from django.contrib import admin

from .models import Group, GroupMembership, User, UserRole

# Register your models here.
admin.site.register(User)
admin.site.register(Group)
admin.site.register(UserRole)
admin.site.register(GroupMembership)
