# coding: utf-8

from openerp import models, fields, api
import openerp.addons.decimal_precision as dp

# TODO: multi-company awareness to be developed


class StockCard(models.TransientModel):
    _name = 'stock.card'
    product_ids = fields.Many2many('product.product', string='Products')


class StockCardProduct(models.TransientModel):
    _name = 'stock.card.product'
    product_id = fields.Many2one('product.product', string='Product')
    stock_card_move_ids = fields.One2many(
        'stock.card.move', 'stock_card_product_id', 'Product Moves')

    @api.multi
    def stock_card_move_get(self):
        self.ensure_one()
        if not (self.product_id.valuation == 'real_time' and
                self.product_id.cost_method in ('average', 'real')):
            return True
        self.stock_card_move_ids.unlink()
        self._stock_card_move_get(self.product_id.id)

        return True

    def _stock_card_move_get(self, product_id, return_values=False):
        scm_obj = self.env['stock.card.move']
        sm_obj = self.env['stock.move']
        self.stock_card_move_ids.unlink()
        product_qty = 0.0
        average = 0.0
        inventory_valuation = 0.0
        lines = []
        avg_move_dict = {}
        for row in self._stock_card_move_history_get(product_id):
            dst = row['dst_usage']
            src = row['src_usage']
            move_id = row['move_id']
            move_brw = sm_obj.browse(move_id)
            if dst == 'internal':
                direction = 1
            else:
                direction = -1
            qty = row['product_qty']
            product_qty += (direction * qty)

            self._cr.execute(
                '''
                SELECT cost, qty
                FROM stock_quant_move_rel AS sqm_rel
                INNER JOIN stock_quant AS sq ON sq.id = sqm_rel.quant_id
                WHERE sqm_rel.move_id = %s
                ''', (move_id,)
                )
            values = self._cr.fetchall()

            # TODO: What is to be done with `procurement` & `view`

            if dst in ('customer', 'production', 'inventory', 'transit'):
                # TODO: move to `transit` could be a return
                # average is kept unchanged products are taken at average price
                avg_move_dict[move_id] = average
                move_valuation = sum([average * val[1] for val in values])
                # NOTE: For production
                # a) it could be a consumption: if so average is kept unchanged
                # products are taken at average price
                # TODO: Consider the case that
                # b) it could be a return: defective good, reworking, etc.

            if dst in ('supplier',):
                # Cost is the one record in the stock_move, cost in the
                # quant record includes other segmentation cost: landed_cost,
                # material_cost, production_cost, subcontracting_cost
                # Inventory Value has to be decreased by the amount of purchase
                # TODO: BEWARE price_unit needs to be normalised
                move_valuation = sum([move_brw.price_unit * val[1]
                                     for val in values])

            if src in ('supplier', 'production', 'inventory', 'transit'):
                # TODO: transit could be a return that shall be recorded at
                # average cost of transaction
                # average is to be computed considering all the segmentation
                # costs inside quant
                move_valuation = sum([val[0] * val[1] for val in values])

            if src in ('customer',):
                # NOTE: Identify the originating move_id of returning move
                origin_id = move_brw.origin_returned_move_id.id
                # NOTE: Falling back to average in case customer return is
                # orphan, i.e., return was created from scratch
                old_average = avg_move_dict.get(origin_id, 0.0) or average
                move_valuation = sum([old_average * val[1] for val in values])

            cost_unit = move_valuation / qty if qty else 0.0
            inventory_valuation += direction * move_valuation
            average = (product_qty and inventory_valuation / product_qty or
                       average)
            if return_values:
                continue
            lines.append(dict(
                date=row['date'],
                move_id=move_id,
                stock_card_product_id=self.id,
                product_qty=product_qty,
                qty=direction * qty,
                move_valuation=direction * move_valuation,
                inventory_valuation=inventory_valuation,
                average=average,
                cost_unit=cost_unit,
                ))
        if return_values:
            return dict(average=average, product_qty=product_qty)
        for line in lines:
            scm_obj.create(line)

        return True

    def get_average(self, product_id):
        values = self._stock_card_move_get(product_id, return_values=True)
        return values.get('average')

    def get_qty(self, product_id):
        values = self._stock_card_move_get(product_id, return_values=True)
        return values.get('product_qty')

    @api.multi
    def action_view_moves(self):
        '''
        This function returns an action that display existing invoices of given
        commission payment ids. It can either be a in a list or in a form view,
        if there is only one invoice to show.
        '''
        self.ensure_one()
        ctx = self._context.copy()

        ir_model_obj = self.pool['ir.model.data']
        model, action_id = ir_model_obj.get_object_reference(
            self._cr, self._uid, 'stock_card', 'stock_card_move_action')
        action = self.pool[model].read(
            self._cr, self._uid, action_id, context=self._context)
        action['context'] = ctx
        # compute the number of invoices to display
        scm_ids = [scm_brw.id for scm_brw in self.stock_card_move_ids]
        # choose the view_mode accordingly
        if len(scm_ids) >= 1:
            action['domain'] = "[('id','in',[" + ','.join(
                [str(scm_id) for scm_id in scm_ids]
            ) + "])]"
        else:
            action['domain'] = "[('id','in',[])]"
        return action

    def _stock_card_move_history_get(self, product_id):
        self._cr.execute(
            '''
            SELECT
                sm.id AS move_id, sm.date, sm.product_id, prod.product_tmpl_id,
                sm.product_qty, sl_src.usage AS src_usage,
                sl_dst.usage AS dst_usage,
                ir_prop_cost.value_text AS cost_method,
                sm.date AS date
            FROM stock_move AS sm
            INNER JOIN
                stock_location AS sl_src ON sm.location_id = sl_src.id
            INNER JOIN
                stock_location AS sl_dst ON sm.location_dest_id = sl_dst.id
            INNER JOIN
                 product_product AS prod ON sm.product_id = prod.id
            INNER JOIN
                product_template AS ptemp ON prod.product_tmpl_id = ptemp.id
            INNER JOIN
                ir_property AS ir_prop_cost ON (
                    ir_prop_cost.res_id = 'product.template,' ||
                    ptemp.id::text and ir_prop_cost.name = 'cost_method')
            WHERE
                sm.state = 'done' -- Stock Move already DONE
                AND ir_prop_cost.value_text = 'average' -- Average Products
                AND sl_src.usage != sl_dst.usage -- No self transfers
                AND (
                    (sl_src.usage = 'internal' AND sl_dst.usage != 'internal')
                    OR (
                    sl_src.usage != 'internal' AND sl_dst.usage = 'internal')
                ) -- Actual incoming or outgoing Stock Moves
                AND sm.product_id = %s
            ORDER BY sm.date
            ''', (product_id,)
        )
        return self._cr.dictfetchall()


class StockCardMove(models.TransientModel):
    _name = 'stock.card.move'

    stock_card_product_id = fields.Many2one(
        'stock.card.product', string='Stock Card Product')
    move_id = fields.Many2one('stock.move', string='Stock Moves')
    product_qty = fields.Float('Inventory Quantity')
    qty = fields.Float('Move Quantity')
    move_valuation = fields.Float(
        string='Move Valuation',
        digits=dp.get_precision('Account'),
        readonly=True)
    inventory_valuation = fields.Float(
        string='Inventory Valuation',
        digits=dp.get_precision('Account'),
        readonly=True)
    average = fields.Float(
        string='Average',
        digits=dp.get_precision('Account'),
        readonly=True)
    cost_unit = fields.Float(
        string='Unit Cost',
        digits=dp.get_precision('Account'),
        readonly=True)
    date = fields.Datetime(string='Date')
