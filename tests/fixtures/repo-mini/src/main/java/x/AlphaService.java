package x;

public class AlphaService {

    private final BetaService betaService = new BetaService();

    public Alpha saveAlpha(Alpha alpha) {
        if (alpha.getId() == null) {
            Context.requirePrivilege("ADD_ALPHA");
        }
        String mode = Config.get(Constants.GP_ALPHA_MODE);
        betaService.saveBeta(alpha);
        return alpha;
    }

    public void purgeAlpha(Alpha alpha) {
        if (alpha.inUse()) {
            throw new APIException("Alpha.purge.inUse");
        }
    }

    public Alpha voidAlpha(Alpha alpha, String reason) {
        return saveAlpha(alpha);
    }

    public Alpha unvoidAlpha(Alpha alpha) {
        return saveAlpha(alpha);
    }
}
