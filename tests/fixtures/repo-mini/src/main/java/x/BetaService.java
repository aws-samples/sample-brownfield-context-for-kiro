package x;

public class BetaService {

    public void saveBeta(Alpha alpha) {
        int limit = Integer.parseInt(Config.get(Constants.GP_BETA_LIMIT));
        if (limit < 0) {
            throw new APIException("Beta.limit.exceeded");
        }
    }

    public void checkBeta() {
        saveBeta(null);
    }
}
