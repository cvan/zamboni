import amo
from amo.tests import AppFactory
from market.models import AddonPremium, Price
from mkt.webapps.models import Webapp


class PaidAppMixin(object):

    def setup_paid(self, type_=None):
        type_ = amo.ADDON_PREMIUM if type_ is None else type_
        self.free = [Webapp.objects.get(id=337141), AppFactory.create()]
        self.paid = []
        for x in xrange(1, 3):
            app = AppFactory.create(weekly_downloads=x * 100)
            if type_ in amo.ADDON_PREMIUMS:
                price = Price.objects.create(price=x)
                AddonPremium.objects.create(price=price, addon=app)
                app.update(premium_type=type_)
                self.paid.append(app)
            elif type_ in amo.ADDON_FREES:
                self.free.append(app)

        # For measure add some disabled free apps ...
        AppFactory.create(disabled_by_user=True)
        AppFactory.create(status=amo.STATUS_NULL)

        # ... and some disabled paid apps.
        price = Price.objects.create(price='50.00')
        a = AppFactory.create(disabled_by_user=True, premium_type=amo.ADDON_PREMIUM)
        AddonPremium.objects.create(price=price, addon=a)
        a = AppFactory.create(status=amo.STATUS_NULL, premium_type=amo.ADDON_PREMIUM)
        AddonPremium.objects.create(price=price, addon=a)

        self.both = sorted(self.free + self.paid,
                           key=lambda x: x.weekly_downloads, reverse=True)
        self.free = sorted(self.free, key=lambda x: x.weekly_downloads,
                           reverse=True)
        self.paid = sorted(self.paid, key=lambda x: x.weekly_downloads,
                           reverse=True)
